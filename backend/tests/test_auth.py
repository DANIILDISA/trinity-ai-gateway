import hashlib
import re
from collections.abc import Generator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import delete as sa_delete

from app.models.audit_log import AdminAuditLog
from app.models.notification import Notification
from app.models.password_reset_token import PasswordResetToken
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services.email_service import MockEmailService, get_email_service

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
LOGOUT_URL = "/api/v1/auth/logout"
RECOVER_URL = "/api/v1/auth/recover"

_TEST_EMAILS = [
    "test_register@example.com",
    "test_dup@example.com",
    "test_hash@example.com",
    "test_login@example.com",
    "test_blocked@example.com",
    "test_admin@example.com",
]

VALID_PAYLOAD: dict = {
    "email": "test_register@example.com",
    "password": "SecurePass1",
    "display_name": "Тест",
}

_LOGIN_CREDS: dict = {
    "email": "test_login@example.com",
    "password": "SecurePass1",
}

_LOGIN_REGISTER_PAYLOAD: dict = {
    **_LOGIN_CREDS,
    "display_name": "ЛогинТест",
}


@pytest_asyncio.fixture(autouse=True)
async def clean_test_users(db_session: AsyncSession) -> None:
    for email in _TEST_EMAILS:
        result = await db_session.execute(
            select(User).where(User.email_lower == email.lower())
        )
        user = result.scalar_one_or_none()
        if user is not None:
            # admin_audit_log + notifications have no ON DELETE CASCADE — clear first.
            # (Notification rows may leak in from cross-file fan-out tests.)
            await db_session.execute(
                sa_delete(Notification).where(Notification.user_id == user.id)
            )
            await db_session.execute(
                sa_delete(AdminAuditLog).where(AdminAuditLog.actor_id == user.id)
            )
            await db_session.delete(user)
    await db_session.commit()


# ── Tests 1-4: Registration ───────────────────────────────────────────────────

async def test_valid_registration(http_client: AsyncClient) -> None:
    response = await http_client.post(REGISTER_URL, json=VALID_PAYLOAD)
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    user = data["user"]
    assert user["balance"] == "0.0000"
    assert user["email"] == VALID_PAYLOAD["email"]
    assert user["role"] == "user"
    assert user["status"] == "active"
    assert "id" in user
    assert user["display_name"] == VALID_PAYLOAD["display_name"]


async def test_duplicate_email_409(http_client: AsyncClient) -> None:
    payload = {**VALID_PAYLOAD, "email": "test_dup@example.com"}
    r1 = await http_client.post(REGISTER_URL, json=payload)
    assert r1.status_code == 200
    r2 = await http_client.post(REGISTER_URL, json=payload)
    assert r2.status_code == 409


async def test_short_password_422(http_client: AsyncClient) -> None:
    payload = {**VALID_PAYLOAD, "password": "short"}
    response = await http_client.post(REGISTER_URL, json=payload)
    assert response.status_code == 422


async def test_password_stored_as_bcrypt_hash(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    payload = {**VALID_PAYLOAD, "email": "test_hash@example.com"}
    response = await http_client.post(REGISTER_URL, json=payload)
    assert response.status_code == 200

    result = await db_session.execute(
        select(User).where(User.email_lower == payload["email"].lower())
    )
    user = result.scalar_one_or_none()
    assert user is not None
    assert user.password_hash is not None
    assert user.password_hash != payload["password"]
    assert user.password_hash.startswith("$2b$")


async def test_register_sets_refresh_cookie(http_client: AsyncClient) -> None:
    response = await http_client.post(REGISTER_URL, json=VALID_PAYLOAD)
    assert response.status_code == 200
    # refresh token is delivered as an httpOnly cookie, not in the body
    assert response.json().get("refresh_token") is None
    assert response.cookies.get("refresh_token") is not None


async def test_register_cookie_is_persistent_with_sliding_window(
    http_client: AsyncClient,
) -> None:
    response = await http_client.post(REGISTER_URL, json=VALID_PAYLOAD)
    assert response.status_code == 200
    max_age = _cookie_max_age(_refresh_cookie_header(response))
    assert 7 * 86400 - 120 <= max_age <= 7 * 86400


async def test_register_creates_refresh_row_with_absolute_cap(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await http_client.post(REGISTER_URL, json=VALID_PAYLOAD)
    raw = response.cookies.get("refresh_token")
    assert raw is not None

    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    row = result.scalar_one()
    now = datetime.now(timezone.utc)
    assert timedelta(days=13, hours=23) < row.absolute_expires_at - now <= timedelta(days=14)
    assert timedelta(days=6, hours=23) < row.expires_at - now <= timedelta(days=7)


async def test_register_is_atomic_if_post_insert_step_fails(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after the user INSERT (e.g. building the refresh token) must NOT
    leave a durable orphan user — that would 500 /register on retry (unique email)
    yet silently work via /login. register must be all-or-nothing like login."""
    import app.services.auth_service as auth_service_mod
    from app.schemas.auth import RegisterRequest

    def _boom() -> str:
        raise RuntimeError("simulated failure after user insert")

    monkeypatch.setattr(auth_service_mod, "create_refresh_token", _boom)

    payload = RegisterRequest(
        email="test_register@example.com", password="SecurePass1", display_name="Тест"
    )
    with pytest.raises(RuntimeError):
        await auth_service_mod.register_user(db_session, payload)

    # Roll back the aborted request's session, then confirm nothing was committed.
    await db_session.rollback()
    result = await db_session.execute(
        select(User).where(User.email_lower == "test_register@example.com")
    )
    assert result.scalar_one_or_none() is None


# ── Login & refresh tokens ───────────────────────────────────────────────────

async def test_valid_login(http_client: AsyncClient) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    response = await http_client.post(LOGIN_URL, json=_LOGIN_CREDS)
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data.get("refresh_token") is None  # not in body
    assert response.cookies.get("refresh_token") is not None  # set as httpOnly cookie
    assert data["token_type"] == "bearer"
    assert data["user"]["email"] == _LOGIN_CREDS["email"]


def _refresh_cookie_header(response: Response) -> str:
    headers = response.headers.get_list("set-cookie")
    matches = [h for h in headers if h.startswith("refresh_token=")]
    assert matches, f"no refresh_token Set-Cookie in {headers}"
    return matches[0]


def _cookie_max_age(header: str) -> int:
    m = re.search(r"max-age=(\d+)", header, re.IGNORECASE)
    assert m, f"refresh cookie must be persistent (Max-Age), got: {header}"
    return int(m.group(1))


async def test_login_cookie_is_persistent_with_sliding_window(
    http_client: AsyncClient,
) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    response = await http_client.post(LOGIN_URL, json=_LOGIN_CREDS)
    assert response.status_code == 200

    max_age = _cookie_max_age(_refresh_cookie_header(response))
    assert 7 * 86400 - 120 <= max_age <= 7 * 86400


async def test_login_sets_absolute_expiry_cap(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    response = await http_client.post(LOGIN_URL, json=_LOGIN_CREDS)
    raw = response.cookies.get("refresh_token")
    assert raw is not None

    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    row = result.scalar_one()
    now = datetime.now(timezone.utc)
    assert timedelta(days=13, hours=23) < row.absolute_expires_at - now <= timedelta(days=14)
    assert timedelta(days=6, hours=23) < row.expires_at - now <= timedelta(days=7)


async def test_recover_cookie_is_persistent(http_client: AsyncClient) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    response = await http_client.post(RECOVER_URL, json=_LOGIN_CREDS)
    assert response.status_code == 200
    max_age = _cookie_max_age(_refresh_cookie_header(response))
    assert 7 * 86400 - 120 <= max_age <= 7 * 86400


async def test_wrong_password_401(http_client: AsyncClient) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    response = await http_client.post(
        LOGIN_URL, json={**_LOGIN_CREDS, "password": "WrongPass9"}
    )
    assert response.status_code == 401


async def test_unknown_email_401(http_client: AsyncClient) -> None:
    response = await http_client.post(
        LOGIN_URL, json={"email": "nobody@example.com", "password": "SomePass1"}
    )
    assert response.status_code == 401


async def test_refresh_valid_token(http_client: AsyncClient) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    login_resp = await http_client.post(LOGIN_URL, json=_LOGIN_CREDS)
    assert login_resp.status_code == 200
    refresh_token = login_resp.cookies.get("refresh_token")
    assert refresh_token is not None

    response = await http_client.post(REFRESH_URL, cookies={"refresh_token": refresh_token})
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["user"]["email"] == _LOGIN_CREDS["email"]
    assert response.cookies.get("refresh_token") is not None


async def test_refresh_revoked_token_401(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    login_resp = await http_client.post(LOGIN_URL, json=_LOGIN_CREDS)
    assert login_resp.status_code == 200
    refresh_token = login_resp.cookies.get("refresh_token")
    assert refresh_token is not None

    # Directly delete the token row — simulates revocation without using logout
    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    row = result.scalar_one_or_none()
    if row is not None:
        await db_session.delete(row)
        await db_session.commit()

    response = await http_client.post(REFRESH_URL, cookies={"refresh_token": refresh_token})
    assert response.status_code == 401


async def test_logout_then_refresh_401(http_client: AsyncClient) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    login_resp = await http_client.post(LOGIN_URL, json=_LOGIN_CREDS)
    assert login_resp.status_code == 200
    access_token = login_resp.json()["access_token"]
    refresh_token = login_resp.cookies.get("refresh_token")
    assert refresh_token is not None

    logout_resp = await http_client.post(
        LOGOUT_URL,
        cookies={"refresh_token": refresh_token},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert logout_resp.status_code == 200

    response = await http_client.post(REFRESH_URL, cookies={"refresh_token": refresh_token})
    assert response.status_code == 401


# ── Refresh rotation tests ────────────────────────────────────────────────────

async def test_refresh_rotation_deletes_old_token(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    login_resp = await http_client.post(LOGIN_URL, json=_LOGIN_CREDS)
    old_raw = login_resp.cookies.get("refresh_token")
    assert old_raw is not None
    old_hash = hashlib.sha256(old_raw.encode()).hexdigest()

    await http_client.post(REFRESH_URL, cookies={"refresh_token": old_raw})

    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == old_hash)
    )
    assert result.scalar_one_or_none() is None


async def _login_and_get_row(
    http_client: AsyncClient, db_session: AsyncSession
) -> tuple[str, RefreshToken]:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    login_resp = await http_client.post(LOGIN_URL, json=_LOGIN_CREDS)
    raw = login_resp.cookies.get("refresh_token")
    assert raw is not None
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    return raw, result.scalar_one()


async def _row_by_raw(db_session: AsyncSession, raw: str) -> RefreshToken | None:
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    return result.scalar_one_or_none()


async def test_refresh_rotation_inherits_absolute_expiry(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    old_raw, old_row = await _login_and_get_row(http_client, db_session)
    original_absolute = old_row.absolute_expires_at

    refresh_resp = await http_client.post(REFRESH_URL, cookies={"refresh_token": old_raw})
    assert refresh_resp.status_code == 200
    new_raw = refresh_resp.cookies.get("refresh_token")
    assert new_raw is not None

    new_row = await _row_by_raw(db_session, new_raw)
    assert new_row is not None
    assert new_row.absolute_expires_at == original_absolute


async def test_refresh_slides_expiry_window(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    old_raw, old_row = await _login_and_get_row(http_client, db_session)
    now = datetime.now(timezone.utc)
    old_row.expires_at = now + timedelta(days=1)
    old_row.absolute_expires_at = now + timedelta(days=10)
    await db_session.commit()

    refresh_resp = await http_client.post(REFRESH_URL, cookies={"refresh_token": old_raw})
    assert refresh_resp.status_code == 200
    new_raw = refresh_resp.cookies.get("refresh_token")
    assert new_raw is not None
    new_row = await _row_by_raw(db_session, new_raw)
    assert new_row is not None
    # window slid from +1d back out to ~+7d, NOT inherited
    assert new_row.expires_at - now > timedelta(days=6, hours=23)
    max_age = _cookie_max_age(_refresh_cookie_header(refresh_resp))
    assert 7 * 86400 - 120 <= max_age <= 7 * 86400


async def test_refresh_clamps_to_absolute_cap(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    old_raw, old_row = await _login_and_get_row(http_client, db_session)
    now = datetime.now(timezone.utc)
    cap = now + timedelta(days=2)
    old_row.expires_at = cap
    old_row.absolute_expires_at = cap
    await db_session.commit()

    refresh_resp = await http_client.post(REFRESH_URL, cookies={"refresh_token": old_raw})
    assert refresh_resp.status_code == 200
    new_raw = refresh_resp.cookies.get("refresh_token")
    assert new_raw is not None
    new_row = await _row_by_raw(db_session, new_raw)
    assert new_row is not None
    assert new_row.expires_at == new_row.absolute_expires_at
    max_age = _cookie_max_age(_refresh_cookie_header(refresh_resp))
    assert max_age <= 2 * 86400


async def test_refresh_past_absolute_cap_401_and_deleted(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    old_raw, old_row = await _login_and_get_row(http_client, db_session)
    now = datetime.now(timezone.utc)
    old_row.expires_at = now + timedelta(days=1)  # sliding window still open
    old_row.absolute_expires_at = now - timedelta(seconds=1)  # cap passed
    await db_session.commit()

    response = await http_client.post(REFRESH_URL, cookies={"refresh_token": old_raw})
    assert response.status_code == 401
    assert await _row_by_raw(db_session, old_raw) is None


async def test_refresh_expired_token_401(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    login_resp = await http_client.post(LOGIN_URL, json=_LOGIN_CREDS)
    raw = login_resp.cookies.get("refresh_token")
    assert raw is not None
    token_hash = hashlib.sha256(raw.encode()).hexdigest()

    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    row = result.scalar_one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    response = await http_client.post(REFRESH_URL, cookies={"refresh_token": raw})
    assert response.status_code == 401

    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    assert result.scalar_one_or_none() is None


async def test_refresh_token_reuse_401(http_client: AsyncClient) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    login_resp = await http_client.post(LOGIN_URL, json=_LOGIN_CREDS)
    old_raw = login_resp.cookies.get("refresh_token")
    assert old_raw is not None

    first = await http_client.post(REFRESH_URL, cookies={"refresh_token": old_raw})
    assert first.status_code == 200

    reuse = await http_client.post(REFRESH_URL, cookies={"refresh_token": old_raw})
    assert reuse.status_code == 401


# ── JTI blacklist tests ───────────────────────────────────────────────────────

async def test_logout_blacklists_access_token(http_client: AsyncClient) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    login_resp = await http_client.post(LOGIN_URL, json=_LOGIN_CREDS)
    assert login_resp.status_code == 200
    access_token = login_resp.json()["access_token"]
    refresh_token = login_resp.cookies.get("refresh_token")
    assert refresh_token is not None

    logout_resp = await http_client.post(
        LOGOUT_URL,
        cookies={"refresh_token": refresh_token},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert logout_resp.status_code == 200

    retry_resp = await http_client.post(
        LOGOUT_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert retry_resp.status_code == 401


async def test_logout_without_refresh_cookie_200(http_client: AsyncClient) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    login_resp = await http_client.post(LOGIN_URL, json=_LOGIN_CREDS)
    assert login_resp.status_code == 200
    access_token = login_resp.json()["access_token"]

    logout_resp = await http_client.post(
        LOGOUT_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert logout_resp.status_code == 200


# ── Authorization middleware tests ────────────────────────────────────────────

_AUTH_ONLY_URL = "/api/v1/auth/test/auth-only"
_ACTIVE_ONLY_URL = "/api/v1/auth/test/active-only"
_ADMIN_ONLY_URL = "/api/v1/auth/test/admin-only"

_BLOCKED_CREDS: dict = {"email": "test_blocked@example.com", "password": "SecurePass1"}
_BLOCKED_REGISTER: dict = {**_BLOCKED_CREDS, "display_name": "Заблокирован"}
_ADMIN_CREDS: dict = {"email": "test_admin@example.com", "password": "SecurePass1"}
_ADMIN_REGISTER: dict = {**_ADMIN_CREDS, "display_name": "Администратор"}


async def test_require_auth_unauthenticated_401(http_client: AsyncClient) -> None:
    response = await http_client.get(_AUTH_ONLY_URL)
    assert response.status_code == 401


async def test_require_auth_active_user_200(http_client: AsyncClient) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    login_resp = await http_client.post(LOGIN_URL, json=_LOGIN_CREDS)
    access_token = login_resp.json()["access_token"]

    response = await http_client.get(
        _AUTH_ONLY_URL, headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


async def test_require_auth_blocked_user_200(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    await http_client.post(REGISTER_URL, json=_BLOCKED_REGISTER)
    result = await db_session.execute(
        select(User).where(User.email_lower == _BLOCKED_CREDS["email"])
    )
    user = result.scalar_one()
    user.status = "blocked"
    await db_session.commit()

    login_resp = await http_client.post(LOGIN_URL, json=_BLOCKED_CREDS)
    assert login_resp.status_code == 200
    access_token = login_resp.json()["access_token"]

    response = await http_client.get(
        _AUTH_ONLY_URL, headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 200


async def test_require_active_blocked_user_403(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    await http_client.post(REGISTER_URL, json=_BLOCKED_REGISTER)
    result = await db_session.execute(
        select(User).where(User.email_lower == _BLOCKED_CREDS["email"])
    )
    user = result.scalar_one()
    user.status = "blocked"
    await db_session.commit()

    login_resp = await http_client.post(LOGIN_URL, json=_BLOCKED_CREDS)
    access_token = login_resp.json()["access_token"]

    response = await http_client.get(
        _ACTIVE_ONLY_URL, headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Аккаунт заблокирован"


async def test_require_admin_non_admin_403(http_client: AsyncClient) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    login_resp = await http_client.post(LOGIN_URL, json=_LOGIN_CREDS)
    access_token = login_resp.json()["access_token"]

    response = await http_client.get(
        _ADMIN_ONLY_URL, headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Доступ запрещён"


async def test_require_admin_admin_user_200(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    await http_client.post(REGISTER_URL, json=_ADMIN_REGISTER)
    result = await db_session.execute(
        select(User).where(User.email_lower == _ADMIN_CREDS["email"])
    )
    user = result.scalar_one()
    user.role = "admin"
    await db_session.commit()

    login_resp = await http_client.post(LOGIN_URL, json=_ADMIN_CREDS)
    access_token = login_resp.json()["access_token"]

    response = await http_client.get(
        _ADMIN_ONLY_URL, headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


async def test_last_active_at_updated(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    login_resp = await http_client.post(LOGIN_URL, json=_LOGIN_CREDS)
    access_token = login_resp.json()["access_token"]

    before = datetime.now(timezone.utc)
    await http_client.get(
        _AUTH_ONLY_URL, headers={"Authorization": f"Bearer {access_token}"}
    )

    result = await db_session.execute(
        select(User).where(User.email_lower == _LOGIN_CREDS["email"])
    )
    user = result.scalar_one()
    assert user.last_active_at is not None
    assert user.last_active_at >= before


# ── Password reset tests ──────────────────────────────────────────────────────

RESET_REQUEST_URL = "/api/v1/auth/reset-password/request"
RESET_CONFIRM_URL = "/api/v1/auth/reset-password/confirm"


@pytest.fixture
def mock_email() -> Generator[MockEmailService, None, None]:
    from app.main import app

    service = MockEmailService()
    app.dependency_overrides[get_email_service] = lambda: service
    yield service
    del app.dependency_overrides[get_email_service]


def _extract_token(body: str) -> str:
    return body.split("token=")[1].strip()


async def test_reset_request_existing_email(
    http_client: AsyncClient,
    db_session: AsyncSession,
    mock_email: MockEmailService,
) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)

    response = await http_client.post(RESET_REQUEST_URL, json={"email": _LOGIN_CREDS["email"]})
    assert response.status_code == 200

    result = await db_session.execute(
        select(User).where(User.email_lower == _LOGIN_CREDS["email"])
    )
    user = result.scalar_one()

    result = await db_session.execute(
        select(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
    )
    token_row = result.scalar_one()
    assert len(token_row.token_hash) == 64  # SHA-256 hex digest
    assert token_row.used_at is None

    assert len(mock_email.sent) == 1
    assert mock_email.sent[0]["to"] == _LOGIN_CREDS["email"]


async def test_reset_request_nonexistent_email_200(
    http_client: AsyncClient,
    mock_email: MockEmailService,
) -> None:
    response = await http_client.post(RESET_REQUEST_URL, json={"email": "nobody@example.com"})
    assert response.status_code == 200
    assert len(mock_email.sent) == 0


async def test_reset_confirm_valid(
    http_client: AsyncClient,
    db_session: AsyncSession,
    mock_email: MockEmailService,
) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    await http_client.post(LOGIN_URL, json=_LOGIN_CREDS)  # creates a refresh token

    await http_client.post(RESET_REQUEST_URL, json={"email": _LOGIN_CREDS["email"]})
    token = _extract_token(mock_email.sent[0]["body"])

    response = await http_client.post(
        RESET_CONFIRM_URL, json={"token": token, "new_password": "NewPass99"}
    )
    assert response.status_code == 200

    # All refresh tokens must be deleted immediately after confirm (before any new login)
    result = await db_session.execute(
        select(User).where(User.email_lower == _LOGIN_CREDS["email"])
    )
    user = result.scalar_one()
    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.user_id == user.id)
    )
    assert result.scalars().all() == []

    # Old password must not work
    assert (await http_client.post(LOGIN_URL, json=_LOGIN_CREDS)).status_code == 401

    # New password must work
    assert (
        await http_client.post(LOGIN_URL, json={**_LOGIN_CREDS, "password": "NewPass99"})
    ).status_code == 200


async def test_reset_confirm_used_token_400(
    http_client: AsyncClient,
    mock_email: MockEmailService,
) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    await http_client.post(RESET_REQUEST_URL, json={"email": _LOGIN_CREDS["email"]})
    token = _extract_token(mock_email.sent[0]["body"])

    r1 = await http_client.post(
        RESET_CONFIRM_URL, json={"token": token, "new_password": "NewPass99"}
    )
    assert r1.status_code == 200

    r2 = await http_client.post(
        RESET_CONFIRM_URL, json={"token": token, "new_password": "AnotherP1"}
    )
    assert r2.status_code == 400


async def test_reset_confirm_expired_token_400(
    http_client: AsyncClient,
    db_session: AsyncSession,
    mock_email: MockEmailService,
) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    await http_client.post(RESET_REQUEST_URL, json={"email": _LOGIN_CREDS["email"]})
    token = _extract_token(mock_email.sent[0]["body"])

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    result = await db_session.execute(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
    )
    row = result.scalar_one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    response = await http_client.post(
        RESET_CONFIRM_URL, json={"token": token, "new_password": "NewPass99"}
    )
    assert response.status_code == 400


async def test_reset_confirm_short_password_422(
    http_client: AsyncClient,
    mock_email: MockEmailService,
) -> None:
    await http_client.post(REGISTER_URL, json=_LOGIN_REGISTER_PAYLOAD)
    await http_client.post(RESET_REQUEST_URL, json={"email": _LOGIN_CREDS["email"]})
    token = _extract_token(mock_email.sent[0]["body"])

    response = await http_client.post(
        RESET_CONFIRM_URL, json={"token": token, "new_password": "Short1"}
    )
    assert response.status_code == 422
