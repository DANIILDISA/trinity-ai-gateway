import logging
import time
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import RateLimitedError, ServiceUnavailableError
from app.models.alert import Alert
from app.models.system_setting import SystemSetting

logger = logging.getLogger(__name__)

# Back-off (s) advised to a client when the rate-limiter can't reach Redis cleanly.
_REDIS_DEGRADED_RETRY_AFTER = 2

_SETTINGS_CACHE_TTL_SECONDS = 60

# Per BILLING_LOGIC.md §9.1 + SPEC.md §18.2.
_DEFAULT_PER_MINUTE = 10
_DEFAULT_PER_HOUR = 100
_DEFAULT_PER_DAY = 1000

_DEFAULT_ABUSE_THRESHOLD = 10
_ABUSE_WINDOW_SECONDS = 3600

_WINDOW_SECONDS: dict[str, int] = {
    "minute": 60,
    "hour": 3600,
    "day": 86400,
}

# Atomic sliding-window check (audit M9 + tr-09z). One Lua script does the whole
# decision so check + add can never interleave. CHECK-BEFORE-ADD: for each window
# drop expired entries and compare the count to the limit BEFORE adding anything;
# on the FIRST full window, reject (return its 1-based index) WITHOUT adding to any
# window. Only when every window has room is the request added to all. This both
# keeps each window at exactly its limit AND fixes the cross-window leak (tr-09z):
# a request rejected by a narrow window no longer lands in the wider ones. KEYS =
# the three window zsets (minute, hour, day); ARGV = now, member, then
# (width, limit) per window. Returns 0 when admitted.
_CHECK_SCRIPT = """
local now = tonumber(ARGV[1])
local member = ARGV[2]
for i = 0, 2 do
  local key = KEYS[i + 1]
  local width = tonumber(ARGV[3 + i * 2])
  local limit = tonumber(ARGV[4 + i * 2])
  redis.call('ZREMRANGEBYSCORE', key, 0, now - width)
  if redis.call('ZCARD', key) >= limit then
    return i + 1
  end
end
for i = 0, 2 do
  local key = KEYS[i + 1]
  local width = tonumber(ARGV[3 + i * 2])
  redis.call('ZADD', key, now, member)
  redis.call('EXPIRE', key, width)
end
return 0
"""


async def _get_limit(
    db: AsyncSession, redis: Redis, key: str, default: int
) -> int:
    """Read an int setting through a Redis cache.

    Mirrors token_estimator._get_cached_float_setting but returns int.
    Cache key: settings:{key}. TTL 60s — admin changes propagate within a
    minute (SPEC.md §18.2).
    """
    cache_key = f"settings:{key}"
    cached = await redis.get(cache_key)
    if cached is not None:
        return int(cached)

    raw = (
        await db.execute(
            select(SystemSetting.value).where(SystemSetting.key == key)
        )
    ).scalar_one_or_none()
    value = int(raw) if raw is not None else default
    await redis.setex(cache_key, _SETTINGS_CACHE_TTL_SECONDS, str(value))
    return value


def _rl_key(user_id: UUID, window: str) -> str:
    return f"ratelimit:{user_id}:{window}"


async def get_retry_after(redis: Redis, user_id: UUID, window: str) -> int:
    """Seconds until the oldest entry in `window` falls out of the sliding window.

    Reads the oldest entry via ZRANGE …WITHSCORES, computes (oldest + width − now).
    Floor of 1 so callers can always sleep at least one second.
    """
    width = _WINDOW_SECONDS[window]
    oldest = await redis.zrange(_rl_key(user_id, window), 0, 0, withscores=True)
    if not oldest:
        return 1
    oldest_ts = float(oldest[0][1])
    wait = int(oldest_ts + width - time.time()) + 1
    return max(wait, 1)


async def check_rate_limit(
    db: AsyncSession, redis: Redis, user_id: UUID
) -> None:
    """Three-window sliding rate-limit check (BILLING_LOGIC.md §9.1).

    One atomic Lua script (`_CHECK_SCRIPT`) drops expired entries and, for each
    window, checks the count against the limit BEFORE adding (check-before-add):
    if any window is full the request is rejected and added to NONE of the
    windows (so a reject never inflates a wider window — tr-09z); otherwise it is
    added to all. `retry_after` is read from the offending window.

    tr-34t3 FU-1: a Redis ConnectionError/TimeoutError propagates to the L2 503
    backstop; any other RedisError (e.g. ResponseError under maxmemory/eviction)
    becomes a graceful 503 (fail-CLOSED — never fail-open, to protect COGS+abuse).
    """
    try:
        per_minute = await _get_limit(db, redis, "rate_limit_per_minute", _DEFAULT_PER_MINUTE)
        per_hour = await _get_limit(db, redis, "rate_limit_per_hour", _DEFAULT_PER_HOUR)
        per_day = await _get_limit(db, redis, "rate_limit_per_day", _DEFAULT_PER_DAY)

        windows = [
            ("minute", _WINDOW_SECONDS["minute"], per_minute),
            ("hour", _WINDOW_SECONDS["hour"], per_hour),
            ("day", _WINDOW_SECONDS["day"], per_day),
        ]

        now = time.time()
        # Unique member per request so ZADD never collides on equal timestamps.
        member = f"{now}:{user_id}"

        keys = [_rl_key(user_id, window) for window, _w, _l in windows]
        argv: list = [now, member]
        for _window, width, limit in windows:
            argv.extend([width, limit])

        rejected = await redis.eval(_CHECK_SCRIPT, len(keys), *keys, *argv)
    except (RedisConnectionError, RedisTimeoutError):
        # Redis unreachable → let the global L2 backstop return a 503 (SB-1).
        raise
    except RedisError as exc:
        # Redis reachable but the op failed (OOM under maxmemory/eviction, etc).
        # Fail-CLOSED graceful 503 — never fail-open (protects OpenRouter COGS + abuse).
        logger.warning("rate_limit check failed (%s: %s) — returning 503", type(exc).__name__, exc)
        raise ServiceUnavailableError(retry_after=_REDIS_DEGRADED_RETRY_AFTER)

    if rejected:
        window = windows[int(rejected) - 1][0]
        # get_retry_after is a best-effort nicety on an already-decided rejection; a Redis
        # error here must NOT downgrade the legitimate 429 to a 503 (tr-34t3 FU-1 audit).
        try:
            retry_after = await get_retry_after(redis, user_id, window)
        except RedisError:
            retry_after = 1
        raise RateLimitedError(
            message=f"Слишком много запросов. Попробуйте через {retry_after} секунд",
            retry_after=retry_after,
        )


async def check_rate_limit_abuse(
    db: AsyncSession, redis: Redis, user_id: UUID
) -> None:
    """Per BILLING_LOGIC.md §9.2: count rate-limit hits per user per hour.

    When hits cross the configured threshold AND no rate-limiter alert for
    this user exists from the last hour, write a single new Alert. The
    "no alert in last hour" check is enforced via a Redis flag with a 1h
    TTL — simpler and cheaper than a SQL window scan.

    tr-34t3 FU-1: same Redis-degradation policy as check_rate_limit — Connection/
    Timeout propagate to the L2 503; other RedisError → graceful 503.
    """
    try:
        threshold = await _get_limit(
            db, redis, "rate_limit_abuse_threshold", _DEFAULT_ABUSE_THRESHOLD
        )

        key = f"ratelimit:abuse:{user_id}"
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, _ABUSE_WINDOW_SECONDS)

        if count <= threshold:
            return

        alert_flag_key = f"ratelimit:abuse_alert:{user_id}"
        # SET NX with EX is atomic — only the first crosser within the hour wins.
        just_set = await redis.set(alert_flag_key, "1", ex=_ABUSE_WINDOW_SECONDS, nx=True)
    except (RedisConnectionError, RedisTimeoutError):
        raise
    except RedisError as exc:
        logger.warning("rate_limit abuse check failed (%s: %s) — returning 503", type(exc).__name__, exc)
        raise ServiceUnavailableError(retry_after=_REDIS_DEGRADED_RETRY_AFTER)

    if not just_set:
        return

    db.add(
        Alert(
            severity="warning",
            source="rate_limiter",
            title="Rate limit abuse",
            message=f"User {user_id} hit rate limits {count} times in the last hour.",
            details={"user_id": str(user_id), "hits": count, "threshold": threshold},
        )
    )
    await db.commit()
