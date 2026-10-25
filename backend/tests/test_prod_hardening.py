"""Prod-deploy hardening: the in-process daily-reconciliation scheduler (tr-376l)
and the production OpenAPI-docs gate (tr-uufc). Mirrors
test_reservation_cleanup_scheduler.py for the loop tests."""
import asyncio
from unittest.mock import AsyncMock

import pytest

import app.main as main_module


async def test_daily_reconciliation_loop_sleeps_then_runs_then_propagates_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recon = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(main_module, "run_daily_reconciliation", recon)

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await main_module._daily_reconciliation_loop()

    assert recon.await_count == 1
    assert slept == [main_module._DAILY_RECONCILIATION_INTERVAL_SECONDS]


async def test_daily_reconciliation_loop_survives_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recon = AsyncMock(side_effect=[RuntimeError("transient blip"), asyncio.CancelledError])
    monkeypatch.setattr(main_module, "run_daily_reconciliation", recon)

    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await main_module._daily_reconciliation_loop()

    # First tick raised (swallowed), second raised CancelledError (propagated).
    assert recon.await_count == 2


async def test_run_daily_reconciliation_safe_swallows_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recon = AsyncMock(side_effect=RuntimeError("cold db"))
    monkeypatch.setattr(main_module, "run_daily_reconciliation", recon)
    # Must NOT raise — a startup reconciliation blip only warns.
    await main_module._run_daily_reconciliation_safe()
    assert recon.await_count == 1


async def test_run_daily_reconciliation_safe_runs_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recon = AsyncMock(return_value=None)
    monkeypatch.setattr(main_module, "run_daily_reconciliation", recon)
    await main_module._run_daily_reconciliation_safe()
    assert recon.await_count == 1


# ── tr-uufc: OpenAPI docs disabled in production ──────────────────────────────


def test_openapi_docs_disabled_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module.settings, "app_env", "production")
    app = main_module.create_app()
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


def test_openapi_docs_enabled_in_development(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module.settings, "app_env", "development")
    app = main_module.create_app()
    assert app.docs_url == "/api/docs"
    assert app.redoc_url == "/api/redoc"
    assert app.openapi_url == "/openapi.json"
