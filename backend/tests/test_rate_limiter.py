"""Rate-limiter sliding-window tests per BILLING_LOGIC.md §9.1/§9.2."""
import asyncio
import time
import uuid

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import RateLimitedError
from app.models.alert import Alert
from app.services.rate_limiter import (
    _rl_key,
    check_rate_limit,
    check_rate_limit_abuse,
    get_retry_after,
)

_SETTINGS_KEYS = (
    "settings:rate_limit_per_minute",
    "settings:rate_limit_per_hour",
    "settings:rate_limit_per_day",
    "settings:rate_limit_abuse_threshold",
)


@pytest_asyncio.fixture
async def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture(autouse=True)
async def _clear_state(redis_client: Redis, db_session: AsyncSession):
    """Cold-start every test: drop cached settings + any leftover alerts."""
    for key in _SETTINGS_KEYS:
        await redis_client.delete(key)
    await db_session.execute(
        sa_delete(Alert).where(Alert.source == "rate_limiter")
    )
    await db_session.commit()
    yield
    for key in _SETTINGS_KEYS:
        await redis_client.delete(key)


async def _clear_user_keys(redis: Redis, user_id: uuid.UUID) -> None:
    keys = [
        _rl_key(user_id, "minute"),
        _rl_key(user_id, "hour"),
        _rl_key(user_id, "day"),
        f"ratelimit:abuse:{user_id}",
        f"ratelimit:abuse_alert:{user_id}",
    ]
    await redis.delete(*keys)


async def test_requests_within_limit(
    db_session: AsyncSession, redis_client: Redis, user_id: uuid.UUID
) -> None:
    await _clear_user_keys(redis_client, user_id)
    # Default per_minute = 10 → exactly 10 should succeed.
    for _ in range(10):
        await check_rate_limit(db_session, redis_client, user_id)


async def test_11th_request_rejected(
    db_session: AsyncSession, redis_client: Redis, user_id: uuid.UUID
) -> None:
    await _clear_user_keys(redis_client, user_id)
    for _ in range(10):
        await check_rate_limit(db_session, redis_client, user_id)

    with pytest.raises(RateLimitedError) as exc_info:
        await check_rate_limit(db_session, redis_client, user_id)

    err = exc_info.value
    assert err.retry_after is not None
    assert err.retry_after >= 1
    # Per-minute window → retry < 65s once we factor a tiny slack.
    assert err.retry_after <= 65


async def test_retry_after_accurate(
    db_session: AsyncSession, redis_client: Redis, user_id: uuid.UUID
) -> None:
    await _clear_user_keys(redis_client, user_id)
    # Inject 10 entries with timestamps ~50s in the past for the minute window.
    past = time.time() - 50
    key = _rl_key(user_id, "minute")
    mapping = {f"{past + i * 0.001}:{user_id}-{i}": past + i * 0.001 for i in range(10)}
    await redis_client.zadd(key, mapping)
    await redis_client.expire(key, 60)

    retry_after = await get_retry_after(redis_client, user_id, "minute")
    # Oldest = past (≈ 50s ago), width = 60 → expires in ~10s.
    assert 8 <= retry_after <= 12, f"expected ~10s, got {retry_after}"


async def test_window_expiry(
    db_session: AsyncSession, redis_client: Redis, user_id: uuid.UUID
) -> None:
    await _clear_user_keys(redis_client, user_id)
    # Inject 10 stale entries (5 minutes ago) for the minute window —
    # ZREMRANGEBYSCORE should drop them on the next check, so a fresh
    # request must succeed.
    stale = time.time() - 300
    key = _rl_key(user_id, "minute")
    mapping = {f"{stale + i * 0.001}:stale-{i}": stale + i * 0.001 for i in range(10)}
    await redis_client.zadd(key, mapping)
    await redis_client.expire(key, 60)

    # Should not raise — stale entries are evicted by the check.
    await check_rate_limit(db_session, redis_client, user_id)

    # And only the one fresh entry remains in the minute window.
    remaining = await redis_client.zcard(key)
    assert remaining == 1


async def test_abuse_alert_created(
    db_session: AsyncSession, redis_client: Redis, user_id: uuid.UUID
) -> None:
    await _clear_user_keys(redis_client, user_id)
    # 11 hits > default threshold of 10 — alert must materialize once.
    for _ in range(11):
        await check_rate_limit_abuse(db_session, redis_client, user_id)

    rows = (
        await db_session.execute(
            select(Alert).where(Alert.source == "rate_limiter")
        )
    ).scalars().all()
    matching = [r for r in rows if r.details and r.details.get("user_id") == str(user_id)]
    assert len(matching) == 1, f"expected one alert, got {len(matching)}"

    alert = matching[0]
    assert alert.severity == "warning"
    assert "Rate limit" in alert.title
    assert alert.details["hits"] >= 11
    assert alert.details["threshold"] == 10

    # Additional hits within the same hour must not spawn duplicate alerts.
    for _ in range(5):
        await check_rate_limit_abuse(db_session, redis_client, user_id)
    rows2 = (
        await db_session.execute(
            select(Alert).where(Alert.source == "rate_limiter")
        )
    ).scalars().all()
    matching2 = [r for r in rows2 if r.details and r.details.get("user_id") == str(user_id)]
    assert len(matching2) == 1, "duplicate alert created within abuse window"


async def test_rejected_by_narrow_window_not_counted_in_wide(
    db_session: AsyncSession, redis_client: Redis, user_id: uuid.UUID
) -> None:
    # tr-09z: a request rejected by the MINUTE window must NOT be counted in the
    # HOUR/DAY windows (check-before-add adds nothing on reject).
    await _clear_user_keys(redis_client, user_id)
    await redis_client.setex("settings:rate_limit_per_minute", 60, "2")
    await redis_client.setex("settings:rate_limit_per_hour", 60, "100")
    await redis_client.setex("settings:rate_limit_per_day", 60, "1000")

    await check_rate_limit(db_session, redis_client, user_id)  # 1
    await check_rate_limit(db_session, redis_client, user_id)  # 2
    with pytest.raises(RateLimitedError):
        await check_rate_limit(db_session, redis_client, user_id)  # 3 — minute-rejected

    assert await redis_client.zcard(_rl_key(user_id, "minute")) == 2
    assert await redis_client.zcard(_rl_key(user_id, "hour")) == 2
    assert await redis_client.zcard(_rl_key(user_id, "day")) == 2


async def test_wide_window_still_enforced_at_its_limit(
    db_session: AsyncSession, redis_client: Redis, user_id: uuid.UUID
) -> None:
    # tr-09z: no window becomes softer than its limit. With a WIDE minute and a
    # NARROW hour, the hour limit still rejects the 3rd request, and that reject
    # is not counted in the (roomy) minute window either.
    await _clear_user_keys(redis_client, user_id)
    await redis_client.setex("settings:rate_limit_per_minute", 60, "100")
    await redis_client.setex("settings:rate_limit_per_hour", 60, "2")
    await redis_client.setex("settings:rate_limit_per_day", 60, "1000")

    await check_rate_limit(db_session, redis_client, user_id)  # 1
    await check_rate_limit(db_session, redis_client, user_id)  # 2
    with pytest.raises(RateLimitedError) as exc:
        await check_rate_limit(db_session, redis_client, user_id)  # 3 — hour-rejected
    assert exc.value.retry_after >= 1

    assert await redis_client.zcard(_rl_key(user_id, "hour")) == 2
    assert await redis_client.zcard(_rl_key(user_id, "minute")) == 2


async def test_concurrent_requests_enforce_exact_limit(
    db_session: AsyncSession, redis_client: Redis, user_id: uuid.UUID
) -> None:
    # Atomicity (конкурентные инкременты): 20 simultaneous checks against a limit
    # of 10 must admit EXACTLY 10 — no lost updates / double counts. Settings are
    # pre-cached in Redis so the concurrent calls touch no shared DB session.
    await _clear_user_keys(redis_client, user_id)
    await redis_client.setex("settings:rate_limit_per_minute", 60, "10")
    await redis_client.setex("settings:rate_limit_per_hour", 60, "1000")
    await redis_client.setex("settings:rate_limit_per_day", 60, "10000")

    async def one() -> bool:
        try:
            await check_rate_limit(db_session, redis_client, user_id)
            return True
        except RateLimitedError:
            return False

    results = await asyncio.gather(*[one() for _ in range(20)])
    assert sum(results) == 10
    assert await redis_client.zcard(_rl_key(user_id, "minute")) == 10


async def test_partial_window_rollover_frees_slots(
    db_session: AsyncSession, redis_client: Redis, user_id: uuid.UUID
) -> None:
    # Window change (смена окна): 10 entries from 70s ago are outside the 60s
    # minute window; the check evicts them (ZREMRANGEBYSCORE) so a fresh request
    # is admitted, leaving exactly one live entry.
    await _clear_user_keys(redis_client, user_id)
    old = time.time() - 70
    key = _rl_key(user_id, "minute")
    await redis_client.zadd(key, {f"{old + i * 0.001}:old-{i}": old + i * 0.001 for i in range(10)})
    await redis_client.expire(key, 120)

    await check_rate_limit(db_session, redis_client, user_id)  # must not raise
    assert await redis_client.zcard(key) == 1


async def test_limits_from_system_settings(
    db_session: AsyncSession, redis_client: Redis, user_id: uuid.UUID
) -> None:
    await _clear_user_keys(redis_client, user_id)
    # Force the cached limit to 2 — any 3rd request must trip the limit.
    await redis_client.setex("settings:rate_limit_per_minute", 60, "2")
    await redis_client.setex("settings:rate_limit_per_hour", 60, "1000")
    await redis_client.setex("settings:rate_limit_per_day", 60, "10000")

    await check_rate_limit(db_session, redis_client, user_id)
    await check_rate_limit(db_session, redis_client, user_id)
    with pytest.raises(RateLimitedError):
        await check_rate_limit(db_session, redis_client, user_id)


# ── tr-34t3 FU-1: Redis-degradation policy (fail-CLOSED 503, not fail-open) ───
from unittest.mock import AsyncMock  # noqa: E402

from redis.exceptions import ConnectionError as _RLConnErr  # noqa: E402
from redis.exceptions import ResponseError as _RLResponseErr  # noqa: E402

from app.core.exceptions import ServiceUnavailableError  # noqa: E402


def _fake_redis_cached_limits(**overrides):
    # redis.get returns a cached limit so _get_limit never touches the db.
    r = AsyncMock()
    r.get = AsyncMock(return_value=b"10")
    for k, v in overrides.items():
        setattr(r, k, v)
    return r


async def test_check_rate_limit_503_on_response_error():
    redis = _fake_redis_cached_limits(eval=AsyncMock(side_effect=_RLResponseErr("OOM command not allowed")))
    with pytest.raises(ServiceUnavailableError) as ei:
        await check_rate_limit(AsyncMock(), redis, uuid.uuid4())
    assert ei.value.http_status == 503
    assert ei.value.retry_after and ei.value.retry_after > 0


async def test_check_rate_limit_reraises_connection_error():
    redis = _fake_redis_cached_limits(eval=AsyncMock(side_effect=_RLConnErr("down")))
    with pytest.raises(_RLConnErr):
        await check_rate_limit(AsyncMock(), redis, uuid.uuid4())


async def test_check_rate_limit_preserves_rate_limited_error():
    redis = _fake_redis_cached_limits(
        eval=AsyncMock(return_value=1),
        zrange=AsyncMock(return_value=[(b"m", 0.0)]),
    )
    with pytest.raises(RateLimitedError):
        await check_rate_limit(AsyncMock(), redis, uuid.uuid4())


async def test_check_rate_limit_429_survives_response_error_in_retry_after():
    # eval already rejected (429); a ResponseError from get_retry_after's zrange must
    # NOT downgrade the legitimate 429 to a 503 (tr-34t3 FU-1 audit).
    redis = _fake_redis_cached_limits(
        eval=AsyncMock(return_value=1),
        zrange=AsyncMock(side_effect=_RLResponseErr("OOM")),
    )
    with pytest.raises(RateLimitedError):
        await check_rate_limit(AsyncMock(), redis, uuid.uuid4())


async def test_check_rate_limit_abuse_503_on_response_error():
    redis = _fake_redis_cached_limits(incr=AsyncMock(side_effect=_RLResponseErr("OOM")))
    with pytest.raises(ServiceUnavailableError):
        await check_rate_limit_abuse(AsyncMock(), redis, uuid.uuid4())


async def test_check_rate_limit_abuse_reraises_connection_error():
    # Symmetry with check_rate_limit: a ConnectionError must propagate to the L2
    # backstop, NOT be converted to a 503 here (audit#2 coverage-gap).
    redis = _fake_redis_cached_limits(incr=AsyncMock(side_effect=_RLConnErr("down")))
    with pytest.raises(_RLConnErr):
        await check_rate_limit_abuse(AsyncMock(), redis, uuid.uuid4())
