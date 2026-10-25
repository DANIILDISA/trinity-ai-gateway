"""Lifespan wiring for the CBR exchange-rate auto-update scheduler.

The gating logic (enabled flag + interval throttle) lives in
run_exchange_rate_update itself and is covered elsewhere; here we only verify
that the lifespan helper actually invokes it.
"""
from unittest.mock import AsyncMock

import pytest

import app.main as main_module


async def test_run_exchange_rate_once_invokes_updater(
    http_client,  # triggers init_engine/init_redis for the global handles
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = AsyncMock(return_value={"status": "disabled"})
    monkeypatch.setattr(main_module, "run_exchange_rate_update", called)

    await main_module._run_exchange_rate_once()

    assert called.await_count == 1
    # Called with a DB session and a Redis handle.
    assert len(called.await_args.args) == 2
