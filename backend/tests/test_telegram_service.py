"""Tests for the Telegram alert dispatcher (Task 9.3)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest

from app.services import telegram_service


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def telegram_configured(monkeypatch: pytest.MonkeyPatch):
    """Wire up token + chat_id and force TELEGRAM_DEMO_MODE off so the HTTP path runs."""
    monkeypatch.setattr(telegram_service.settings, "telegram_demo_mode", False)
    monkeypatch.setattr(
        telegram_service.settings, "telegram_bot_token", "test_token_abc"
    )
    monkeypatch.setattr(
        telegram_service.settings, "telegram_chat_id", "12345"
    )


@pytest.fixture
def telegram_demo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(telegram_service.settings, "telegram_demo_mode", True)
    monkeypatch.setattr(
        telegram_service.settings, "telegram_bot_token", "test_token_abc"
    )
    monkeypatch.setattr(
        telegram_service.settings, "telegram_chat_id", "12345"
    )


# ── HTTP mock ───────────────────────────────────────────────────────────────


class _MockResponse:
    def __init__(self, status_code: int = 200, text: str = '{"ok":true}') -> None:
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", "https://api.telegram.org/botX/sendMessage"),
                response=httpx.Response(self.status_code, text=self.text),
            )


class _MockAsyncClient:
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

    async def post(self, url: str, *, json: dict) -> _MockResponse:
        self.calls.append({"url": url, "json": json})
        if self._raise is not None:
            raise self._raise
        return self._response


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, mock: _MockAsyncClient) -> None:
    monkeypatch.setattr(
        telegram_service.httpx,
        "AsyncClient",
        lambda *a, **kw: mock,
    )


async def _flush_tasks() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


# ── Severity prefix routing ─────────────────────────────────────────────────


async def test_critical_alert_sends_with_red_prefix(
    telegram_configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock = _MockAsyncClient()
    _patch_httpx(monkeypatch, mock)

    await telegram_service.send_telegram_alert("Balance below threshold", "critical")
    await _flush_tasks()

    assert len(mock.calls) == 1
    call = mock.calls[0]
    assert call["url"] == (
        "https://api.telegram.org/bottest_token_abc/sendMessage"
    )
    assert call["json"]["chat_id"] == "12345"
    text = call["json"]["text"]
    assert text.startswith("🔴 CRITICAL: ")
    assert "Balance below threshold" in text


async def test_warning_alert_sends_with_yellow_prefix(
    telegram_configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock = _MockAsyncClient()
    _patch_httpx(monkeypatch, mock)

    await telegram_service.send_telegram_alert("OpenRouter 5xx rate up", "warning")
    await _flush_tasks()

    assert len(mock.calls) == 1
    text = mock.calls[0]["json"]["text"]
    assert text.startswith("🟡 WARNING: ")
    assert "OpenRouter 5xx rate up" in text


async def test_info_alert_does_not_call_telegram(
    telegram_configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock = _MockAsyncClient()
    _patch_httpx(monkeypatch, mock)

    await telegram_service.send_telegram_alert("FYI metric tick", "info")
    await _flush_tasks()

    assert mock.calls == []


# ── Severity is case-insensitive ────────────────────────────────────────────


async def test_uppercase_severity_accepted(
    telegram_configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BILLING_LOGIC.md §14.1 calls send_telegram_alert with severity='CRITICAL'."""
    mock = _MockAsyncClient()
    _patch_httpx(monkeypatch, mock)

    await telegram_service.send_telegram_alert("uppercase severity", "CRITICAL")
    await _flush_tasks()

    assert len(mock.calls) == 1
    assert mock.calls[0]["json"]["text"].startswith("🔴 CRITICAL: ")


# ── Error tolerance ─────────────────────────────────────────────────────────


async def test_telegram_http_error_logged_not_raised(
    telegram_configured,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock = _MockAsyncClient(response=_MockResponse(status_code=500, text="boom"))
    _patch_httpx(monkeypatch, mock)
    caplog.set_level(logging.ERROR, logger=telegram_service.logger.name)

    await telegram_service.send_telegram_alert("trigger 500", "critical")
    await _flush_tasks()

    assert any("Telegram HTTP 500" in r.message for r in caplog.records)


async def test_telegram_network_error_logged_not_raised(
    telegram_configured,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock = _MockAsyncClient(raise_exc=httpx.ConnectError("no route"))
    _patch_httpx(monkeypatch, mock)
    caplog.set_level(logging.ERROR, logger=telegram_service.logger.name)

    await telegram_service.send_telegram_alert("trigger net err", "warning")
    await _flush_tasks()

    assert any("Telegram send failed" in r.message for r in caplog.records)


# ── Config missing → skipped silently ───────────────────────────────────────


async def test_missing_token_skips_silently(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(telegram_service.settings, "telegram_demo_mode", False)
    monkeypatch.setattr(telegram_service.settings, "telegram_bot_token", "")
    monkeypatch.setattr(telegram_service.settings, "telegram_chat_id", "12345")

    mock = _MockAsyncClient()
    _patch_httpx(monkeypatch, mock)
    caplog.set_level(logging.WARNING, logger=telegram_service.logger.name)

    await telegram_service.send_telegram_alert("nowhere to go", "critical")
    await _flush_tasks()

    assert mock.calls == []
    assert any(
        "token or chat_id not configured" in r.message for r in caplog.records
    )


async def test_missing_chat_id_skips_silently(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(telegram_service.settings, "telegram_demo_mode", False)
    monkeypatch.setattr(telegram_service.settings, "telegram_bot_token", "tok")
    monkeypatch.setattr(telegram_service.settings, "telegram_chat_id", "")

    mock = _MockAsyncClient()
    _patch_httpx(monkeypatch, mock)
    caplog.set_level(logging.WARNING, logger=telegram_service.logger.name)

    await telegram_service.send_telegram_alert("no chat", "warning")
    await _flush_tasks()

    assert mock.calls == []
    assert any(
        "token or chat_id not configured" in r.message for r in caplog.records
    )


# ── DEMO_MODE skips HTTP and logs ───────────────────────────────────────────


async def test_demo_mode_skips_http_and_logs(
    telegram_demo,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock = _MockAsyncClient()
    _patch_httpx(monkeypatch, mock)
    caplog.set_level(logging.INFO, logger=telegram_service.logger.name)

    await telegram_service.send_telegram_alert("demo run", "critical")
    await telegram_service.send_telegram_alert("demo run 2", "warning")
    await _flush_tasks()

    assert mock.calls == []
    demo = [r for r in caplog.records if "[DEMO] Telegram alert would be sent" in r.message]
    assert len(demo) == 2
