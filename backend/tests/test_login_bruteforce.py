"""tr-dfem: login brute-force protection — integration behaviour."""
import uuid

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.auth_service as auth_service_module
from app.config import settings
from app.models.user import User

pytestmark = pytest.mark.asyncio

LOGIN_URL = "/api/v1/auth/login"
REGISTER_URL = "/api/v1/auth/register"


def _unique_email() -> str:
    return f"brute-{uuid.uuid4().hex}@example.com"


async def _register(http_client: AsyncClient, email: str, password: str = "SecurePass1") -> None:
    resp = await http_client.post(REGISTER_URL, json={
        "email": email, "password": password, "display_name": "Тест",
    })
    assert resp.status_code == 200, resp.text


async def _fail_login(http_client: AsyncClient, email: str) -> Response:
    return await http_client.post(LOGIN_URL, json={"email": email, "password": "WrongPass1"})


async def _make_admin(db_session: AsyncSession, email: str) -> None:
    await db_session.execute(
        update(User).where(User.email_lower == email.lower()).values(role="admin")
    )
    await db_session.commit()


async def test_unknown_email_still_runs_bcrypt(http_client: AsyncClient, monkeypatch):
    """User enumeration via timing: a non-existent account must cost one bcrypt
    verify, same as a wrong password on a real account."""
    calls = {"n": 0}
    real_verify = auth_service_module.verify_password

    async def counting_verify(plain: str, hashed: str) -> bool:
        calls["n"] += 1
        return await real_verify(plain, hashed)

    monkeypatch.setattr(auth_service_module, "verify_password", counting_verify)
    resp = await http_client.post(LOGIN_URL, json={
        "email": f"ghost-{uuid.uuid4().hex}@example.com",
        "password": "SecurePass1",
    })
    assert resp.status_code == 401
    assert calls["n"] == 1


async def test_block_after_threshold_even_with_correct_password(http_client: AsyncClient):
    email = _unique_email()
    await _register(http_client, email)
    for _ in range(settings.login_fail_threshold):
        resp = await _fail_login(http_client, email)
    assert resp.status_code == 429  # the threshold-crossing attempt is itself refused
    # Correct password while blocked → still 429 with Retry-After.
    resp = await http_client.post(LOGIN_URL, json={"email": email, "password": "SecurePass1"})
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) > 0
    assert "попыток" in resp.json()["detail"]


async def test_admin_blocked_after_stricter_threshold(
    http_client: AsyncClient, db_session: AsyncSession
):
    email = _unique_email()
    await _register(http_client, email)
    await _make_admin(db_session, email)
    for i in range(settings.login_fail_threshold_admin - 1):
        resp = await _fail_login(http_client, email)
        assert resp.status_code == 401, f"attempt {i + 1} should still be 401"
    resp = await _fail_login(http_client, email)
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= settings.login_block_steps_admin_seconds[0] - 5


async def test_success_resets_counter(http_client: AsyncClient):
    email = _unique_email()
    await _register(http_client, email)
    for _ in range(settings.login_fail_threshold - 1):
        await _fail_login(http_client, email)
    resp = await http_client.post(LOGIN_URL, json={"email": email, "password": "SecurePass1"})
    assert resp.status_code == 200
    # Counter is reset: threshold-1 more failures do NOT block.
    for _ in range(settings.login_fail_threshold - 1):
        resp = await _fail_login(http_client, email)
    assert resp.status_code == 401


async def test_unknown_email_is_throttled_too(http_client: AsyncClient):
    """Non-existent accounts must throttle identically — otherwise the 429/401
    difference enumerates users."""
    email = _unique_email()  # never registered
    for _ in range(settings.login_fail_threshold):
        resp = await _fail_login(http_client, email)
    assert resp.status_code == 429


# ── Redis outage: fail-open (users) / fail-closed (admins) ───────────────────

from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.exceptions import ServiceUnavailableError
from app.schemas.auth import LoginRequest
from app.services.auth_service import login_user
from app.services.email_service import MockEmailService


class _DownRedis:
    """Every command raises — simulates a Redis outage."""
    def __getattr__(self, name):
        async def _raise(*args, **kwargs):
            raise RedisConnectionError("connection refused")
        return _raise


async def test_regular_user_fails_open_when_redis_down(
    http_client: AsyncClient, db_session: AsyncSession
):
    email = _unique_email()
    await _register(http_client, email)
    svc = MockEmailService()
    auth_response, _, _ = await login_user(
        db_session, _DownRedis(), LoginRequest(email=email, password="SecurePass1"),
        svc, ip_address="10.0.0.1",
    )
    assert auth_response.user.email == email


async def test_admin_fails_closed_when_redis_down(
    http_client: AsyncClient, db_session: AsyncSession
):
    email = _unique_email()
    await _register(http_client, email)
    await _make_admin(db_session, email)
    with pytest.raises(ServiceUnavailableError):
        await login_user(
            db_session, _DownRedis(), LoginRequest(email=email, password="SecurePass1"),
            MockEmailService(), ip_address="10.0.0.1",
        )


# ── Alerts: admin failures page someone; single user failures don't ──────────

from app.main import app
from app.services.email_service import get_email_service


async def test_admin_failed_attempt_sends_alert(
    http_client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(settings, "security_alert_email", "sec@trinity.local")
    email = _unique_email()
    await _register(http_client, email)
    await _make_admin(db_session, email)

    svc = MockEmailService()
    app.dependency_overrides[get_email_service] = lambda: svc
    try:
        resp = await _fail_login(http_client, email)  # ONE failed attempt
    finally:
        app.dependency_overrides.pop(get_email_service, None)
    assert resp.status_code == 401
    assert len(svc.sent) == 1  # alert fires on the FIRST attempt, not the thirtieth
    assert "admin_login_failed" in svc.sent[0]["subject"]


async def test_regular_user_failure_sends_no_alert(http_client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "security_alert_email", "sec@trinity.local")
    email = _unique_email()
    await _register(http_client, email)
    svc = MockEmailService()
    app.dependency_overrides[get_email_service] = lambda: svc
    try:
        resp = await _fail_login(http_client, email)
    finally:
        app.dependency_overrides.pop(get_email_service, None)
    assert resp.status_code == 401
    assert svc.sent == []  # single failure on a regular account: log/audit only
