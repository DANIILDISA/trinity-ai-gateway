import hashlib
import logging
import math
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete as sa_delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import login_throttle
from app.core.constants import micro_to_api_string
from app.core.security_alerts import notify_security_event
from app.core.exceptions import (
    AccountPendingDeletionError,
    EmailAlreadyRegisteredError,
    ServiceUnavailableError,
)
from app.core.security import (
    blacklist_access_token,
    create_access_token,
    create_refresh_token,
    hash_password,
    verify_password,
)
from app.models.audit_log import AdminAuditLog
from app.models.password_reset_token import PasswordResetToken
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.auth import AuthResponse, LoginRequest, RegisterRequest, UserInAuth
from app.services.email_service import EmailServiceProtocol

logger = logging.getLogger(__name__)


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# tr-dfem: verified against when the email doesn't exist, so response time can't
# distinguish "no such user" from "wrong password" (user enumeration). Lazily
# computed once per process with the same cost factor as real hashes.
_dummy_password_hash: str | None = None


async def _verify_against_dummy(password: str) -> None:
    global _dummy_password_hash
    if _dummy_password_hash is None:
        _dummy_password_hash = await hash_password(secrets.token_hex(16))
    await verify_password(password, _dummy_password_hash)


def _user_to_schema(user: User) -> UserInAuth:
    return UserInAuth(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        full_name=user.full_name,
        role=user.role,
        status=user.status,
        balance=micro_to_api_string(user.balance_micro_rub),
        reserved=micro_to_api_string(user.reserved_micro_rub),
        available=micro_to_api_string(
            max(user.balance_micro_rub - user.reserved_micro_rub, 0)
        ),
        has_password=user.password_hash is not None,
    )


async def register_user(
    db: AsyncSession,
    request: RegisterRequest,
    ip_address: str | None = None,
) -> tuple[AuthResponse, str, datetime]:
    email_lower = request.email.lower()

    result = await db.execute(select(User).where(User.email_lower == email_lower))
    existing = result.scalar_one_or_none()
    if existing is not None:
        # Surface the soft-delete state distinctly so the UI can offer recovery
        # instead of the generic "email already registered" error.
        if existing.scheduled_delete_at is not None:
            raise AccountPendingDeletionError()
        raise EmailAlreadyRegisteredError()

    user = User(
        email=request.email,
        display_name=request.display_name,
        password_hash=await hash_password(request.password),
        role="user",
        status="active",
        balance_micro_rub=0,
        reserved_micro_rub=0,
    )
    db.add(user)
    # flush (not commit) so asyncpg populates the server-generated user.id via
    # RETURNING while keeping the whole registration in ONE transaction: a failure
    # while issuing the refresh token then rolls the user back too, instead of
    # leaving a durable orphan account that 500s /register yet works via /login
    # (audit#2). Mirrors login_user's single commit + the flush-then-commit
    # convention used across the services layer.
    await db.flush()

    # Issue a refresh token immediately (mirrors login_user) so a freshly
    # registered session survives past the 15-min access-token TTL. Same clamp
    # as login: sliding window capped by the absolute expiry.
    raw_token = create_refresh_token()
    now = datetime.now(timezone.utc)
    absolute_expires_at = now + timedelta(
        days=settings.jwt_refresh_token_absolute_expire_days
    )
    expires_at = min(
        now + timedelta(days=settings.jwt_refresh_token_expire_days),
        absolute_expires_at,
    )
    db.add(RefreshToken(
        user_id=user.id,
        token_hash=_token_hash(raw_token),
        expires_at=expires_at,
        absolute_expires_at=absolute_expires_at,
    ))
    db.add(AdminAuditLog(
        actor_id=user.id,
        actor_email=user.email,
        action="auth.register",
        ip_address=ip_address,
    ))
    await db.commit()

    return (
        AuthResponse(
            access_token=create_access_token(str(user.id)),
            user=_user_to_schema(user),
        ),
        raw_token,
        expires_at,
    )


def _raise_throttled(seconds: int) -> None:
    minutes = max(1, math.ceil(seconds / 60))
    raise HTTPException(
        status_code=429,
        detail=f"Слишком много попыток входа. Повторите через {minutes} мин.",
        headers={"Retry-After": str(seconds)},
    )


async def login_user(
    db: AsyncSession,
    redis: Redis,
    request: LoginRequest,
    email_service: EmailServiceProtocol,
    ip_address: str | None = None,
) -> tuple[AuthResponse, str, datetime]:
    email_lower = request.email.lower()

    result = await db.execute(select(User).where(User.email_lower == email_lower))
    user = result.scalar_one_or_none()
    is_admin = user is not None and user.role == "admin"

    # ── Throttle gate (tr-dfem). Runs BEFORE bcrypt: blocked attempts must not
    # burn CPU. Redis down → fail-open for regular users (nginx per-IP limit is
    # the second line), fail-closed for admins (minutes of admin-login downtime
    # beat an unthrottled brute-force window).
    throttle_up = True
    try:
        block_ttl = await login_throttle.get_block_ttl(redis, email_lower)
    except RedisError:
        if is_admin:
            raise ServiceUnavailableError(
                "Вход временно недоступен, повторите попытку позже", retry_after=30
            )
        throttle_up = False
        block_ttl = None
        await notify_security_event(
            email_service, "login_throttle_unavailable",
            email=request.email, ip_address=ip_address,
            detail="Redis недоступен — вход работает без троттлинга (fail-open)",
        )

    if block_ttl is not None:
        _raise_throttled(block_ttl)

    if user is not None and user.password_hash:
        password_valid = await verify_password(request.password, user.password_hash)
    else:
        await _verify_against_dummy(request.password)
        password_valid = False

    if not password_valid:
        blocked_for: int | None = None
        if throttle_up:
            try:
                blocked_for = await login_throttle.register_failure(
                    redis, email_lower, is_admin=is_admin
                )
            except RedisError:
                # Response is 401 either way; losing one counter tick is fine.
                logger.exception("login throttle register_failure failed")
        if is_admin:
            await notify_security_event(
                email_service, "admin_login_failed",
                email=request.email, ip_address=ip_address,
                detail=f"Неудачная попытка входа в админский аккаунт (IP {ip_address})",
            )
        if blocked_for is not None:
            await notify_security_event(
                email_service, "account_blocked",
                email=request.email, ip_address=ip_address,
                detail=f"Аккаунт заблокирован на {blocked_for} с после серии неудачных попыток",
            )
        db.add(AdminAuditLog(
            actor_id=user.id if user is not None else None,
            actor_email=user.email if user is not None else request.email,
            action="auth.login_failed",
            ip_address=ip_address,
        ))
        await db.commit()
        if blocked_for is not None:
            _raise_throttled(blocked_for)
        raise HTTPException(status_code=401, detail="Неверный email или пароль")

    # Password is correct — clear the failure history before any further gates.
    if throttle_up:
        try:
            await login_throttle.reset(redis, email_lower)
        except RedisError:
            logger.exception("login throttle reset failed")

    # Credentials are valid — but the account may be in a soft-delete window.
    # Surface that distinct from a generic auth failure so the frontend can
    # render the recovery prompt instead of "wrong password".
    if user.scheduled_delete_at is not None:
        db.add(AdminAuditLog(
            actor_id=user.id,
            actor_email=user.email,
            action="auth.login_blocked_pending_deletion",
            ip_address=ip_address,
        ))
        await db.commit()
        raise AccountPendingDeletionError()

    raw_token = create_refresh_token()
    now = datetime.now(timezone.utc)
    absolute_expires_at = now + timedelta(
        days=settings.jwt_refresh_token_absolute_expire_days
    )
    # Clamp keeps expires_at <= absolute_expires_at structural, not
    # config-dependent (a sliding window misconfigured longer than the cap
    # would otherwise leak past it into cleanup/session-list/cookie Max-Age).
    expires_at = min(
        now + timedelta(days=settings.jwt_refresh_token_expire_days),
        absolute_expires_at,
    )
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=_token_hash(raw_token),
            expires_at=expires_at,
            absolute_expires_at=absolute_expires_at,
        )
    )
    db.add(AdminAuditLog(
        actor_id=user.id,
        actor_email=user.email,
        action="auth.login",
        ip_address=ip_address,
    ))
    await db.commit()

    return (
        AuthResponse(
            access_token=create_access_token(str(user.id)),
            user=_user_to_schema(user),
        ),
        raw_token,
        expires_at,
    )


# Postgres SQLSTATE for lock_not_available: a FOR UPDATE / lock acquisition that
# exceeded lock_timeout. Checked via sqlstate (not isinstance) because asyncpg's
# LockNotAvailableError reaches us wrapped in SQLAlchemy's dialect Error — the
# sqlstate is propagated onto that wrapper while the raw type is not.
_LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"


def _is_lock_timeout(exc: DBAPIError) -> bool:
    """True when a statement aborted because a row lock wasn't acquired within
    lock_timeout (Postgres 55P03)."""
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate == _LOCK_NOT_AVAILABLE_SQLSTATE


async def refresh_access_token(
    db: AsyncSession,
    refresh_token_str: str,
    ip_address: str | None = None,
) -> tuple[AuthResponse, str, datetime]:
    # Fail fast if a wedged transaction is holding the refresh_tokens/users row
    # locks: SET LOCAL scopes the timeout to THIS transaction, so a blocked
    # FOR UPDATE or commit aborts after refresh_lock_timeout_ms instead of
    # hanging until the request timeout and 503-ing every refresh (tr-wvs). The
    # first execute autobegins the transaction the rotation runs in.
    await db.execute(
        text(f"SET LOCAL lock_timeout = '{settings.refresh_lock_timeout_ms}ms'")
    )
    try:
        return await _rotate_refresh_token(db, refresh_token_str, ip_address)
    except DBAPIError as exc:
        if _is_lock_timeout(exc):
            await db.rollback()
            raise ServiceUnavailableError(
                "Сервис временно недоступен, повторите попытку", retry_after=2
            ) from exc
        raise


async def _rotate_refresh_token(
    db: AsyncSession,
    refresh_token_str: str,
    ip_address: str | None = None,
) -> tuple[AuthResponse, str, datetime]:
    # Row lock serializes concurrent refreshes of the same token: the loser
    # blocks, then re-reads the deleted row and gets 401 instead of minting a
    # second valid token lineage.
    result = await db.execute(
        select(RefreshToken)
        .where(RefreshToken.token_hash == _token_hash(refresh_token_str))
        .with_for_update()
    )
    token_row = result.scalar_one_or_none()

    if token_row is None:
        raise HTTPException(status_code=401, detail="Токен обновления не найден")

    now = datetime.now(timezone.utc)
    if token_row.expires_at < now or token_row.absolute_expires_at < now:
        await db.delete(token_row)
        await db.commit()
        raise HTTPException(status_code=401, detail="Токен обновления истёк")

    absolute_expires_at = token_row.absolute_expires_at

    result = await db.execute(select(User).where(User.id == token_row.user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="Пользователь не найден")

    await db.delete(token_row)

    new_raw_token = create_refresh_token()
    # Sliding window: each refresh extends expiry, but never past the absolute
    # cap set at login — on day 14 the session dies regardless of activity.
    new_expires_at = min(
        now + timedelta(days=settings.jwt_refresh_token_expire_days),
        absolute_expires_at,
    )
    db.add(RefreshToken(
        user_id=user.id,
        token_hash=_token_hash(new_raw_token),
        expires_at=new_expires_at,
        absolute_expires_at=absolute_expires_at,
    ))
    db.add(AdminAuditLog(
        actor_id=user.id,
        actor_email=user.email,
        action="auth.token_refresh",
        ip_address=ip_address,
    ))
    await db.commit()

    return (
        AuthResponse(
            access_token=create_access_token(str(user.id)),
            user=_user_to_schema(user),
        ),
        new_raw_token,
        new_expires_at,
    )


async def logout_user(
    db: AsyncSession,
    redis: Redis,
    access_token_jti: str,
    access_token_exp: int,
    refresh_token_str: str | None = None,
    user: User | None = None,
    ip_address: str | None = None,
) -> None:
    if refresh_token_str is not None:
        result = await db.execute(
            select(RefreshToken).where(RefreshToken.token_hash == _token_hash(refresh_token_str))
        )
        token_row = result.scalar_one_or_none()
        if token_row is not None:
            await db.delete(token_row)

    if user is not None:
        db.add(AdminAuditLog(
            actor_id=user.id,
            actor_email=user.email,
            action="auth.logout",
            ip_address=ip_address,
        ))

    await db.commit()

    # tr-34t3 FU-2: best-effort blocklist write — a Redis Connection/Timeout blip logs a
    # WARNING and lets the sign-out succeed (token expires shortly); ResponseError propagates.
    await blacklist_access_token(redis, access_token_jti, access_token_exp)


async def request_password_reset(
    db: AsyncSession,
    email: str,
    email_service: EmailServiceProtocol,
    ip_address: str | None = None,
) -> None:
    result = await db.execute(select(User).where(User.email_lower == email.lower()))
    user = result.scalar_one_or_none()
    if user is None:
        return  # No enumeration — always succeed silently

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    db.add(PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=expires_at,
    ))
    db.add(AdminAuditLog(
        actor_id=user.id,
        actor_email=user.email,
        action="auth.password_reset_requested",
        ip_address=ip_address,
    ))
    await db.commit()

    reset_link = f"{settings.frontend_url}/reset-password?token={raw_token}"
    await email_service.send_email(
        to=user.email,
        subject="Сброс пароля Trinity",
        body=f"Для сброса пароля перейдите по ссылке:\n{reset_link}",
    )


async def confirm_password_reset(
    db: AsyncSession,
    token: str,
    new_password: str,
    ip_address: str | None = None,
) -> None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    result = await db.execute(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
    )
    token_row = result.scalar_one_or_none()

    if token_row is None or token_row.used_at is not None:
        raise HTTPException(status_code=400, detail="Недействительная ссылка")

    if token_row.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Ссылка истекла")

    result = await db.execute(select(User).where(User.id == token_row.user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=400, detail="Недействительная ссылка")

    user.password_hash = await hash_password(new_password)
    token_row.used_at = datetime.now(timezone.utc)

    await db.execute(sa_delete(RefreshToken).where(RefreshToken.user_id == user.id))

    db.add(AdminAuditLog(
        actor_id=user.id,
        actor_email=user.email,
        action="auth.password_reset",
        ip_address=ip_address,
    ))
    await db.commit()


async def schedule_account_deletion(
    db: AsyncSession,
    user: User,
    ip_address: str | None = None,
) -> None:
    """Mark the user as pending deletion. Existing refresh tokens are revoked
    so all device sessions terminate immediately; the access token will
    expire naturally on its TTL."""
    user.scheduled_delete_at = datetime.now(timezone.utc)
    await db.execute(sa_delete(RefreshToken).where(RefreshToken.user_id == user.id))
    db.add(AdminAuditLog(
        actor_id=user.id,
        actor_email=user.email,
        action="user.account_delete_scheduled",
        ip_address=ip_address,
    ))
    await db.commit()


async def recover_account(
    db: AsyncSession,
    email: str,
    password: str,
    ip_address: str | None = None,
) -> tuple[AuthResponse, str, datetime]:
    """Restore a soft-deleted account on a valid password re-entry.

    Raises 401 for unknown email / wrong password / yandex-only account so the
    error envelope mirrors a normal failed login. On success, clears the
    deletion flag and issues fresh tokens — equivalent to a login.
    """
    result = await db.execute(select(User).where(User.email_lower == email.lower()))
    user = result.scalar_one_or_none()
    if not (user and user.password_hash and await verify_password(password, user.password_hash)):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")

    if user.scheduled_delete_at is None:
        # Idempotent: nothing to recover. Still issue tokens so the UX of
        # "recovery succeeded" works after a race.
        pass
    else:
        user.scheduled_delete_at = None
        db.add(AdminAuditLog(
            actor_id=user.id,
            actor_email=user.email,
            action="user.account_recovered",
            ip_address=ip_address,
        ))

    raw_token = create_refresh_token()
    now = datetime.now(timezone.utc)
    absolute_expires_at = now + timedelta(
        days=settings.jwt_refresh_token_absolute_expire_days
    )
    # Same clamp as login_user — see the comment there.
    expires_at = min(
        now + timedelta(days=settings.jwt_refresh_token_expire_days),
        absolute_expires_at,
    )
    db.add(RefreshToken(
        user_id=user.id,
        token_hash=_token_hash(raw_token),
        expires_at=expires_at,
        absolute_expires_at=absolute_expires_at,
    ))
    db.add(AdminAuditLog(
        actor_id=user.id,
        actor_email=user.email,
        action="auth.login",
        ip_address=ip_address,
    ))
    await db.commit()

    return (
        AuthResponse(
            access_token=create_access_token(str(user.id)),
            user=_user_to_schema(user),
        ),
        raw_token,
        expires_at,
    )
