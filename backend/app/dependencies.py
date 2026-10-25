from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from uuid import UUID as PyUUID

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session_factory
from app.core.exceptions import AuthRequiredError, RateLimitedError
from app.core.redis import get_redis_pool
from app.core.security import decode_access_token
from app.models.user import User
from app.services.rate_limiter import check_rate_limit, check_rate_limit_abuse

_bearer = HTTPBearer()


async def _close_db_session(session: AsyncSession) -> None:
    """Close the request session, invalidating the connection if close fails.

    A clean close returns the connection to the pool for reuse. But if close()
    races an in-flight operation on the connection (asyncpg "another operation is
    in progress") or is cancelled mid-rollback, the connection is in an
    indeterminate state and must be DISCARDED — otherwise it leaks checked-out
    with an open transaction holding row locks, wedging every later request that
    needs those rows (the tr-dus incident). invalidate() drops it from the pool
    rather than returning a poisoned connection. The original error still
    propagates so the failure is surfaced, not silently swallowed. (tr-3sj)
    """
    try:
        await session.close()
    except BaseException:
        try:
            await session.invalidate()
        except Exception:
            # Best-effort: the connection is already being discarded; don't mask
            # the original teardown error with a secondary one.
            pass
        raise


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        await _close_db_session(session)


async def get_redis() -> AsyncGenerator[Redis, None]:
    client = Redis(connection_pool=get_redis_pool())
    try:
        yield client
    finally:
        await client.aclose()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> User:
    try:
        payload = await decode_access_token(credentials.credentials, redis)
        user_id = PyUUID(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise AuthRequiredError()

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise AuthRequiredError()

    return user


async def require_auth(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> User:
    # Throttle the last_active_at write to at most once per 60s per user. Without
    # this, every authenticated read does an UPDATE+commit (a row-lock + WAL write
    # + held DB connection), turning a read workload into a write one and starving
    # the pool under load. SET NX EX is atomic: only the first request in each 60s
    # window wins the key and does the write; the other ~59/60 skip it entirely.
    try:
        just_set = await redis.set(f"last_active:{user.id}", "1", ex=60, nx=True)
    except RedisError:
        # Best-effort throttle: NO Redis failure may 500 an authed request — skip the
        # last_active write. Broadened from Connection/Timeout to all RedisError so an OOM
        # ResponseError (noeviction, tr-5rd7) also fails open. This is a non-essential
        # analytics write; the revocation READ path (decode_access_token) is unchanged.
        just_set = False
    if just_set:
        user.last_active_at = datetime.now(timezone.utc)
        await db.commit()
    return user


async def require_active(
    user: User = Depends(require_auth),
) -> User:
    if user.status != "active":
        raise HTTPException(status_code=403, detail="Аккаунт заблокирован")
    return user


async def require_admin(
    user: User = Depends(require_active),
) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Доступ запрещён")
    return user


async def require_rate_limit(
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> User:
    try:
        await check_rate_limit(db, redis, user.id)
    except RateLimitedError as exc:
        await check_rate_limit_abuse(db, redis, user.id)
        retry_after = exc.retry_after or 1
        raise HTTPException(
            status_code=429,
            detail=exc.message,
            headers={"Retry-After": str(retry_after)},
        )
    return user
