"""tr-dfem: unit tests for the per-account login throttle (pure Redis logic)."""
import uuid

import pytest

from app.config import settings
from app.core import login_throttle

pytestmark = pytest.mark.asyncio


@pytest.fixture
def email() -> str:
    # Unique per test run — no cross-test Redis state, no cleanup needed (TTL'd keys).
    return f"throttle-{uuid.uuid4().hex}@example.com"


async def test_not_blocked_initially(redis_client, email):
    assert await login_throttle.get_block_ttl(redis_client, email) is None


async def test_block_installed_at_threshold(redis_client, email):
    for _ in range(settings.login_fail_threshold - 1):
        assert await login_throttle.register_failure(redis_client, email, is_admin=False) is None
    duration = await login_throttle.register_failure(redis_client, email, is_admin=False)
    assert duration == settings.login_block_steps_seconds[0]
    ttl = await login_throttle.get_block_ttl(redis_client, email)
    assert ttl is not None and 0 < ttl <= duration


async def test_admin_threshold_is_stricter(redis_client, email):
    for _ in range(settings.login_fail_threshold_admin - 1):
        assert await login_throttle.register_failure(redis_client, email, is_admin=True) is None
    duration = await login_throttle.register_failure(redis_client, email, is_admin=True)
    assert duration == settings.login_block_steps_admin_seconds[0]


async def test_repeat_blocks_escalate(redis_client, email):
    async def trip() -> int:
        for _ in range(settings.login_fail_threshold - 1):
            await login_throttle.register_failure(redis_client, email, is_admin=False)
        d = await login_throttle.register_failure(redis_client, email, is_admin=False)
        # Simulate the block expiring so the next round can trip again.
        await redis_client.delete(f"login:block:{email}")
        return d

    assert await trip() == settings.login_block_steps_seconds[0]
    assert await trip() == settings.login_block_steps_seconds[1]
    assert await trip() == settings.login_block_steps_seconds[2]


async def test_escalation_caps_at_last_step(redis_client, email):
    last = settings.login_block_steps_seconds[-1]
    for _ in range(len(settings.login_block_steps_seconds) + 2):
        for _ in range(settings.login_fail_threshold - 1):
            await login_throttle.register_failure(redis_client, email, is_admin=False)
        duration = await login_throttle.register_failure(redis_client, email, is_admin=False)
        await redis_client.delete(f"login:block:{email}")
    assert duration == last


async def test_reset_clears_failures_and_history(redis_client, email):
    for _ in range(settings.login_fail_threshold - 1):
        await login_throttle.register_failure(redis_client, email, is_admin=False)
    await login_throttle.reset(redis_client, email)
    # After reset the counter starts over — threshold-1 more failures still no block.
    for _ in range(settings.login_fail_threshold - 1):
        assert await login_throttle.register_failure(redis_client, email, is_admin=False) is None


async def test_fail_counter_has_window_ttl(redis_client, email):
    await login_throttle.register_failure(redis_client, email, is_admin=False)
    ttl = await redis_client.ttl(f"login:fail:{email}")
    assert 0 < ttl <= settings.login_fail_window_seconds
