import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AdminAuditLog
from app.models.user import User

REGISTER_URL = "/api/v1/auth/register"
HEALTH_URL = "/api/v1/health"
WEBHOOK_URL = "/api/v1/payments/webhook/yookassa"

_CSRF_EMAIL = "test_csrf@example.com"
_CSRF_PAYLOAD: dict = {
    "email": _CSRF_EMAIL,
    "password": "SecurePass1",
    "display_name": "CSRFTest",
}


@pytest_asyncio.fixture(autouse=True)
async def clean_csrf_user(db_session: AsyncSession) -> None:
    result = await db_session.execute(select(User).where(User.email_lower == _CSRF_EMAIL))
    user = result.scalar_one_or_none()
    if user is not None:
        await db_session.execute(
            sa_delete(AdminAuditLog).where(AdminAuditLog.actor_id == user.id)
        )
        await db_session.delete(user)
    await db_session.commit()


async def test_csrf_invalid_token_403(http_client: AsyncClient) -> None:
    response = await http_client.post(
        REGISTER_URL,
        json=_CSRF_PAYLOAD,
        headers={"X-CSRF-Token": "bogus-token-not-in-redis"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid CSRF token"


async def test_csrf_valid_token_passes(http_client: AsyncClient, csrf_token: str) -> None:
    response = await http_client.post(REGISTER_URL, json=_CSRF_PAYLOAD)
    assert response.status_code == 200


async def test_csrf_get_not_required(http_client: AsyncClient) -> None:
    response = await http_client.get(HEALTH_URL)
    assert response.status_code == 200
    assert "X-CSRF-Token" in response.headers


async def test_csrf_webhook_path_exempt(http_client: AsyncClient) -> None:
    response = await http_client.post(
        WEBHOOK_URL,
        json={},
        headers={"X-CSRF-Token": "bogus-token-not-in-redis"},
    )
    assert response.status_code != 403


# ── tr-34t3 FU-5: CSRF 503 uses the standard error envelope ──────────────────
from redis.exceptions import ConnectionError as _CsrfRedisConnErr  # noqa: E402


async def test_csrf_503_uses_standard_error_envelope(http_client: AsyncClient, monkeypatch) -> None:
    # Redis down during a mutating request → 503 with {"error":{"code":...}} + Retry-After,
    # not the legacy {"detail": ...}. Patch the name where the middleware USES it
    # (bound via `from app.core.redis import get_redis_client` at import time).
    class _FailingRedis:
        async def exists(self, *a, **k):
            raise _CsrfRedisConnErr("down")
        async def setex(self, *a, **k):
            raise _CsrfRedisConnErr("down")

    monkeypatch.setattr("app.core.middleware.get_redis_client", lambda: _FailingRedis())
    response = await http_client.post(REGISTER_URL, json=_CSRF_PAYLOAD)
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "redis_unavailable"
    assert response.headers.get("Retry-After")


async def test_csrf_503_header_check_branch_standard_envelope(
    http_client: AsyncClient, monkeypatch
) -> None:
    # Branch 2: the setup block succeeds (redis_ok=True), the inner header-token exists()
    # raises — must ALSO use the standard envelope, not the legacy {"detail"} (audit#2 gap).
    _HDR = "header-token-that-fails"

    class _StatefulRedis:
        async def exists(self, key):
            if _HDR in key:            # header-token validation → raise
                raise _CsrfRedisConnErr("down")
            return 1                    # cookie-token check → succeed (redis_ok=True)
        async def setex(self, *a, **k):
            return True

    monkeypatch.setattr("app.core.middleware.get_redis_client", lambda: _StatefulRedis())
    response = await http_client.post(
        REGISTER_URL, json=_CSRF_PAYLOAD, headers={"X-CSRF-Token": _HDR}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "redis_unavailable"
    assert response.headers.get("Retry-After")


# ── tr-w4fy.2: CSRF-middleware responses must pass through CORSMiddleware ─────
# CSRFMiddleware used to sit OUTSIDE CORSMiddleware, so its self-made 503/403
# never got Access-Control-Allow-Origin — the browser reported an opaque
# network error (net::ERR_FAILED) and the frontend showed «Ошибка сети»
# instead of the server's RU message.

_ORIGIN = {"Origin": "http://localhost:3000"}


async def test_csrf_503_carries_cors_headers(http_client: AsyncClient, monkeypatch) -> None:
    class _FailingRedis:
        async def exists(self, *a, **k):
            raise _CsrfRedisConnErr("down")

        async def setex(self, *a, **k):
            raise _CsrfRedisConnErr("down")

    monkeypatch.setattr("app.core.middleware.get_redis_client", lambda: _FailingRedis())
    response = await http_client.post(REGISTER_URL, json=_CSRF_PAYLOAD, headers=_ORIGIN)
    assert response.status_code == 503
    assert (
        response.headers.get("access-control-allow-origin") == "http://localhost:3000"
    )


async def test_csrf_403_carries_cors_headers(http_client: AsyncClient) -> None:
    response = await http_client.post(
        REGISTER_URL,
        json=_CSRF_PAYLOAD,
        headers={"X-CSRF-Token": "bogus-token-not-in-redis", **_ORIGIN},
    )
    assert response.status_code == 403
    assert (
        response.headers.get("access-control-allow-origin") == "http://localhost:3000"
    )
