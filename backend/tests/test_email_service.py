"""Tests for the Resend-backed email service (Task 9.1)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services import email_service


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def disable_demo(monkeypatch: pytest.MonkeyPatch):
    """Force EMAIL_DEMO_MODE off so the HTTP code path is exercised."""
    monkeypatch.setattr(email_service.settings, "email_demo_mode", False)
    monkeypatch.setattr(
        email_service.settings, "resend_api_key", "re_test_key", raising=False
    )
    monkeypatch.setattr(
        email_service.settings,
        "resend_from_email",
        "Trinity <test@trinity.local>",
        raising=False,
    )


@pytest.fixture
def enable_demo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(email_service.settings, "email_demo_mode", True)


class _MockResponse:
    def __init__(self, status_code: int = 200, text: str = '{"id":"mock"}') -> None:
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", email_service.RESEND_ENDPOINT),
                response=httpx.Response(self.status_code, text=self.text),
            )


class _MockAsyncClient:
    """Stand-in for httpx.AsyncClient that records POSTs and returns canned
    responses. Supports the ``async with`` protocol."""

    def __init__(
        self,
        *,
        response: _MockResponse | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response or _MockResponse()
        self._raise = raise_exc

    async def __aenter__(self) -> "_MockAsyncClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def post(self, url: str, *, json: dict, headers: dict) -> _MockResponse:
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self._raise is not None:
            raise self._raise
        return self._response


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, mock: _MockAsyncClient) -> None:
    monkeypatch.setattr(
        email_service.httpx,
        "AsyncClient",
        lambda *a, **kw: mock,
    )


async def _flush_tasks() -> None:
    """Yield twice so create_task'd coroutines run to completion."""
    for _ in range(3):
        await asyncio.sleep(0)


# ── Concurrency bound (audit M8) ─────────────────────────────────────────────


async def test_resend_send_bounds_network_concurrency(
    disable_demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    # M8: _resend_send was fire-and-forget with NO concurrency cap — an email
    # burst (mass low-balance/topup notifications) could fan out unbounded
    # concurrent Resend calls. A module Semaphore must cap in-flight sends.
    EXPECTED_CAP = 20  # mirrors _EMAIL_MAX_CONCURRENCY in email_service
    gate = asyncio.Event()
    state = {"live": 0, "max": 0}

    class _GatedClient:
        async def __aenter__(self) -> "_GatedClient":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, url: str, *, json: dict, headers: dict) -> _MockResponse:
            state["live"] += 1
            state["max"] = max(state["max"], state["live"])
            await gate.wait()
            state["live"] -= 1
            return _MockResponse()

    monkeypatch.setattr(
        email_service.httpx, "AsyncClient", lambda *a, **kw: _GatedClient()
    )

    n = EXPECTED_CAP + 5
    tasks = [
        asyncio.ensure_future(
            email_service._resend_send(f"u{i}@example.com", "s", "t", "h")
        )
        for i in range(n)
    ]
    # With the gate held, exactly CAP tasks acquire the semaphore + enter post;
    # the rest block on the semaphore. Yield enough for them to settle.
    for _ in range(20):
        await asyncio.sleep(0)
    try:
        assert state["max"] == EXPECTED_CAP, f"max concurrent = {state['max']}"
        assert state["live"] == EXPECTED_CAP  # rest are waiting on the semaphore
    finally:
        gate.set()
        await asyncio.gather(*tasks)
    assert state["live"] == 0
    assert state["max"] == EXPECTED_CAP  # never exceeded the cap


# ── Each typed helper sends to Resend without raising ───────────────────────


async def test_send_password_reset_posts_to_resend(
    disable_demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock = _MockAsyncClient()
    _patch_httpx(monkeypatch, mock)

    await email_service.send_password_reset(
        "user@example.com", "https://trinity.local/reset?token=abc"
    )
    await _flush_tasks()

    assert len(mock.calls) == 1
    call = mock.calls[0]
    assert call["url"] == email_service.RESEND_ENDPOINT
    assert call["headers"]["Authorization"] == "Bearer re_test_key"
    body = call["json"]
    assert body["to"] == ["user@example.com"]
    assert body["subject"] == "Сброс пароля Trinity"
    assert "https://trinity.local/reset?token=abc" in body["text"]
    assert "https://trinity.local/reset?token=abc" in body["html"]


async def test_send_low_balance_posts_to_resend(
    disable_demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock = _MockAsyncClient()
    _patch_httpx(monkeypatch, mock)

    await email_service.send_low_balance(
        "user@example.com", remaining_requests=42, topup_link="https://t/topup"
    )
    await _flush_tasks()

    assert len(mock.calls) == 1
    body = mock.calls[0]["json"]
    assert body["subject"] == "Низкий баланс Trinity"
    assert "42" in body["text"]
    assert "https://t/topup" in body["html"]


async def test_send_topup_confirmed_posts_to_resend(
    disable_demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock = _MockAsyncClient()
    _patch_httpx(monkeypatch, mock)

    await email_service.send_topup_confirmed(
        "user@example.com", amount=500.0, new_balance=1234.56
    )
    await _flush_tasks()

    body = mock.calls[0]["json"]
    assert body["subject"] == "Пополнение баланса Trinity"
    assert "500.00" in body["text"]
    assert "1234.56" in body["text"]


async def test_send_tariff_change_posts_to_resend(
    disable_demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock = _MockAsyncClient()
    _patch_httpx(monkeypatch, mock)

    await email_service.send_tariff_change(
        "user@example.com", old_tariff="Старт", new_tariff="Премиум"
    )
    await _flush_tasks()

    body = mock.calls[0]["json"]
    assert body["subject"] == "Изменение тарифов Trinity"
    assert "Старт" in body["text"]
    assert "Премиум" in body["text"]


# ── DEMO_MODE skips the HTTP call ───────────────────────────────────────────


async def test_demo_mode_skips_http_call(
    enable_demo, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    mock = _MockAsyncClient()
    _patch_httpx(monkeypatch, mock)

    caplog.set_level(logging.INFO, logger=email_service.logger.name)

    await email_service.send_password_reset("user@example.com", "https://x/r")
    await email_service.send_low_balance("user@example.com", 10, "https://x/t")
    await email_service.send_topup_confirmed("user@example.com", 100.0, 200.0)
    await email_service.send_tariff_change("user@example.com", "A", "B")
    await _flush_tasks()

    assert mock.calls == []
    demo_logs = [r for r in caplog.records if "[DEMO] Email would be sent" in r.message]
    assert len(demo_logs) == 4


# ── Failed HTTP → logged, not raised ────────────────────────────────────────


async def test_resend_http_error_logged_not_raised(
    disable_demo, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    mock = _MockAsyncClient(response=_MockResponse(status_code=500, text="server boom"))
    _patch_httpx(monkeypatch, mock)
    caplog.set_level(logging.ERROR, logger=email_service.logger.name)

    # Must not raise.
    await email_service.send_password_reset("user@example.com", "https://x/r")
    await _flush_tasks()

    err = [r for r in caplog.records if "Resend HTTP 500" in r.message]
    assert err, f"expected error log, got {[r.message for r in caplog.records]}"


async def test_resend_network_error_logged_not_raised(
    disable_demo, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    mock = _MockAsyncClient(raise_exc=httpx.ConnectError("no route to host"))
    _patch_httpx(monkeypatch, mock)
    caplog.set_level(logging.ERROR, logger=email_service.logger.name)

    await email_service.send_low_balance("user@example.com", 5, "https://x/t")
    await _flush_tasks()

    err = [r for r in caplog.records if "Resend failed" in r.message]
    assert err, f"expected error log, got {[r.message for r in caplog.records]}"


async def test_missing_api_key_logs_and_does_not_call(
    disable_demo, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(email_service.settings, "resend_api_key", "")
    mock = _MockAsyncClient()
    _patch_httpx(monkeypatch, mock)
    caplog.set_level(logging.ERROR, logger=email_service.logger.name)

    await email_service.send_tariff_change("user@example.com", "A", "B")
    await _flush_tasks()

    assert mock.calls == []
    assert any("RESEND_API_KEY not configured" in r.message for r in caplog.records)


# ── Fire-and-forget: helper returns before HTTP completes ───────────────────


async def test_send_returns_before_http_completes(
    disable_demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    finished = asyncio.Event()

    class _SlowClient(_MockAsyncClient):
        async def post(self, url: str, *, json: dict, headers: dict) -> _MockResponse:
            started.set()
            await asyncio.sleep(0.05)
            finished.set()
            return await super().post(url, json=json, headers=headers)

    mock = _SlowClient()
    _patch_httpx(monkeypatch, mock)

    await email_service.send_password_reset("user@example.com", "https://x/r")
    # The helper has returned, but the network call should still be running.
    assert not finished.is_set(), "send_password_reset should not block on HTTP"
    await asyncio.wait_for(finished.wait(), timeout=1.0)
    assert len(mock.calls) == 1
