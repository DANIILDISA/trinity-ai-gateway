"""tr-2ubv.24: follower re-election / leader failover.

_try_acquire_leadership used to run ONCE per worker at startup: a worker that
booted while the previous leader's 60s lock was still alive (SIGKILL leaves it
behind) — or while Redis was down/OOM — became a PERMANENT follower, and the
whole fleet could end up running NO background tasks (reservation reaper,
reconciliation, model sync, health) until a lucky manual restart.

The lifespan now runs a leadership supervisor: followers retry the acquire on
a timer and promote themselves when the lock frees; a leader that loses the
lock demotes — its leader-only loops are cancelled so a new leader can't
dual-run them — and rejoins the retry loop.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

import app.main as main_module


class _FakeRedis:
    """SET NX per-call script: each .set() pops the next scripted result."""

    def __init__(self, results: list[object]) -> None:
        self._results = results
        self.set_calls = 0

    async def set(self, *args, **kwargs):
        self.set_calls += 1
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


async def test_follower_retries_until_promoted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two failed acquires (lock still held) → keep retrying → promote on the
    third and start the leader tasks exactly once."""
    redis = _FakeRedis([False, False, True])

    started = AsyncMock(return_value=[])
    monkeypatch.setattr(main_module, "_start_leader_tasks", started)

    # Renewal "runs" then is cancelled — ends the supervisor like a shutdown.
    async def cancelled_renew(_redis) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(main_module, "_renew_leadership_loop", cancelled_renew)

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await main_module._leadership_supervisor(redis)

    assert redis.set_calls == 3
    assert started.await_count == 1
    # One follower-retry sleep per failed acquire, at the configured cadence.
    assert slept == [main_module._FOLLOWER_RETRY_INTERVAL_SECONDS] * 2


async def test_lost_leadership_demotes_and_cancels_leader_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the renewal loop reports the lock is gone, the leader-only tasks are
    cancelled (no dual-leader window) and the worker rejoins the retry loop."""
    # Acquire wins, then the post-demotion retry is cancelled (shutdown).
    redis = _FakeRedis([True, asyncio.CancelledError()])

    hung = asyncio.create_task(asyncio.Event().wait())  # a "leader loop"
    monkeypatch.setattr(
        main_module, "_start_leader_tasks", AsyncMock(return_value=[hung])
    )

    async def lost_renew(_redis) -> None:
        return None  # lock expired / taken by someone else

    monkeypatch.setattr(main_module, "_renew_leadership_loop", lost_renew)

    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await main_module._leadership_supervisor(redis)

    assert hung.cancelled()
    assert redis.set_calls == 2


async def test_acquire_error_is_treated_as_follower_and_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Redis blip during acquire (tr-5rd7 ERROR path) must not end the
    supervisor — the worker stays a follower and retries."""
    redis = _FakeRedis([ConnectionError("redis down"), True])

    started = AsyncMock(return_value=[])
    monkeypatch.setattr(main_module, "_start_leader_tasks", started)

    async def cancelled_renew(_redis) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(main_module, "_renew_leadership_loop", cancelled_renew)

    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await main_module._leadership_supervisor(redis)

    assert started.await_count == 1  # promoted on the retry after the blip
