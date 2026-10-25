"""Lifespan wiring for the in-process reservation-cleanup scheduler (tr-u3y5).

The reaper logic (``release_expired_reservations``) and the partial-cost
reconcile (``reconcile_partial_costs``) live in ``app.tasks.cleanup`` and are
covered by ``test_cleanup.py``. Here we only verify that the in-process lifespan
wiring actually invokes them every tick — mirroring
``test_exchange_rate_scheduler.py``.

This is the regression guard for the keystone bug: the reaper exists and is
correct, but was only ever invoked by a host crontab that does not run in the
Docker/Windows deployment, so expired reservations were never swept.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

import app.main as main_module


async def test_run_reservation_cleanup_once_invokes_reaper_and_reconcile(
    http_client,  # triggers init_engine/init_redis so the global factory exists
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The once-helper sweeps expired reservations AND reconciles partial costs,
    each on a DB session (no Redis — unlike the exchange-rate/health helpers)."""
    reaper = AsyncMock(return_value=0)
    reconcile = AsyncMock(return_value=0)
    drift = AsyncMock(return_value=0)
    monkeypatch.setattr(main_module, "release_expired_reservations", reaper)
    monkeypatch.setattr(main_module, "reconcile_partial_costs", reconcile)
    monkeypatch.setattr(main_module, "reconcile_reserved_drift", drift)

    await main_module._run_reservation_cleanup_once()

    assert reaper.await_count == 1
    # Called with a DB session only — one positional arg, no redis handle.
    assert len(reaper.await_args.args) == 1
    assert reconcile.await_count == 1
    assert len(reconcile.await_args.args) == 1
    # Denorm reserved-drift heal runs every tick too (tr-12jl).
    assert drift.await_count == 1
    assert len(drift.await_args.args) == 1


async def test_reservation_cleanup_loop_sleeps_then_sweeps_then_propagates_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop sleeps one tick, runs a sweep, and propagates CancelledError on
    shutdown (so lifespan teardown can cancel it cleanly)."""
    once = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(main_module, "_run_reservation_cleanup_once", once)

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await main_module._reservation_cleanup_loop()

    assert once.await_count == 1
    # Sleep-first cadence at the configured interval, matching every other loop.
    assert slept == [main_module._RESERVATION_CLEANUP_TICK_SECONDS]


async def test_reservation_cleanup_loop_survives_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient sweep failure is swallowed (logged) and the loop keeps ticking
    — a flaky DB blip must never kill the reaper for the life of the process."""
    once = AsyncMock(side_effect=[RuntimeError("transient db blip"), asyncio.CancelledError])
    monkeypatch.setattr(main_module, "_run_reservation_cleanup_once", once)

    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await main_module._reservation_cleanup_loop()

    # First tick raised RuntimeError (swallowed by the broad except), second tick
    # raised CancelledError (propagated) — proving the loop continued past the error.
    assert once.await_count == 2
