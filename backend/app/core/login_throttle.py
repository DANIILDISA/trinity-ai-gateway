"""Per-account login throttling (tr-dfem).

Sliding-window failure counter + exponentially escalating block, keyed on the
normalized email FROM THE REQUEST — including non-existent accounts, so the
throttle's behaviour cannot be used to enumerate users. Pure logic over a
passed-in Redis client: no FastAPI imports, no exception policy — fail-open /
fail-closed decisions live in the caller (auth_service.login_user).
"""
from redis.asyncio import Redis

from app.config import settings

_BLOCKCOUNT_TTL_SECONDS = 86_400  # escalation memory: consecutive blocks within 24h


def _fail_key(email_lower: str) -> str:
    return f"login:fail:{email_lower}"


def _block_key(email_lower: str) -> str:
    return f"login:block:{email_lower}"


def _blockcount_key(email_lower: str) -> str:
    return f"login:blockcount:{email_lower}"


async def get_block_ttl(redis: Redis, email_lower: str) -> int | None:
    """Seconds until the account may retry, or None when not blocked."""
    ttl = await redis.ttl(_block_key(email_lower))
    return ttl if ttl > 0 else None


async def register_failure(redis: Redis, email_lower: str, *, is_admin: bool) -> int | None:
    """Record one failed attempt. Returns the block duration (seconds) when this
    failure crossed the threshold and installed a block, else None."""
    threshold = (
        settings.login_fail_threshold_admin if is_admin else settings.login_fail_threshold
    )
    steps = (
        settings.login_block_steps_admin_seconds
        if is_admin
        else settings.login_block_steps_seconds
    )

    fails = await redis.incr(_fail_key(email_lower))
    if fails == 1:
        await redis.expire(_fail_key(email_lower), settings.login_fail_window_seconds)
    if fails < threshold:
        return None

    block_number = await redis.incr(_blockcount_key(email_lower))
    await redis.expire(_blockcount_key(email_lower), _BLOCKCOUNT_TTL_SECONDS)
    duration = steps[min(block_number - 1, len(steps) - 1)]
    await redis.set(_block_key(email_lower), "1", ex=duration)
    await redis.delete(_fail_key(email_lower))
    return duration


async def reset(redis: Redis, email_lower: str) -> None:
    """Successful login clears the failure counter AND the escalation history."""
    await redis.delete(_fail_key(email_lower), _blockcount_key(email_lower))
