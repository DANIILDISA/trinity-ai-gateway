"""tr-5rd7: under noeviction, a full Redis raises ResponseError (OOM) on denyoom writes
(SET/SETEX/INCR). Request-path writes must degrade gracefully: best-effort/cache writes
fail OPEN, the idempotency gate fails CLOSED (503, tested in test_chat_sse.py), leader
election stays a follower."""
import uuid as _uuid

import pytest
from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import require_auth
from app.models.user import User


class _OOMRedis:
    """Models real Redis under noeviction+OOM: denyoom writes raise; reads succeed."""
    async def set(self, *a, **k):
        raise ResponseError("OOM command not allowed when used memory > 'maxmemory'")
    async def setex(self, *a, **k):
        raise ResponseError("OOM command not allowed when used memory > 'maxmemory'")
    async def get(self, *a, **k):
        return None
    async def exists(self, *a, **k):
        return 0


async def _make_user(db: AsyncSession, email: str) -> User:
    user = User(
        email=email, display_name="OOM", password_hash="x",
        role="user", status="active", balance_micro_rub=0, reserved_micro_rub=0,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def test_require_auth_fails_open_on_redis_oom(db_session: AsyncSession) -> None:
    user = await _make_user(db_session, f"oom_{_uuid.uuid4().hex[:8]}@example.com")
    result = await require_auth(user=user, db=db_session, redis=_OOMRedis())
    assert result is user
    assert user.last_active_at is None  # best-effort write skipped, not applied


async def test_token_estimator_setting_cache_fails_open(db_session: AsyncSession) -> None:
    from app.services.token_estimator import _get_cached_float_setting
    # get→miss, setex→OOM → must return the DB/default value without raising.
    val = await _get_cached_float_setting(
        db_session, _OOMRedis(), "definitely_absent_setting_xyz", 3.14
    )
    assert val == 3.14


async def test_pricing_cache_fails_open(db_session: AsyncSession) -> None:
    from app.services.tariff_service import get_all_current_pricing
    result = await get_all_current_pricing(db_session, _OOMRedis())
    assert isinstance(result, list)  # served from DB despite the cache setex OOM


async def test_try_acquire_leadership_becomes_follower_on_redis_oom() -> None:
    import app.main as main_module
    result = await main_module._try_acquire_leadership(_OOMRedis())
    assert result is False  # graceful follower fallback, never raises
