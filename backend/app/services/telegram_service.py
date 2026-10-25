"""Admin alert dispatcher — SPEC.md §5.5, §13.5.

Fire-and-forget HTTP POST to Telegram Bot API. Critical/warning alerts get a
coloured emoji prefix; info alerts are dashboard-only per BILLING_LOGIC.md §14.1
and never reach Telegram. Missing token or chat_id, network errors, and
non-2xx responses are all logged but never raised — the caller's transaction
must not be derailed by an alerting side-channel.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_TIMEOUT_SECONDS = 10.0

# SPEC.md §5.5: severity prefixes. Accept both upper/lower-case (BILLING_LOGIC.md
# §14.1 calls this with uppercase severity strings, but alert_service receives
# lowercase from the alerts table enum).
_PREFIX_BY_SEVERITY = {
    "critical": "🔴",
    "warning": "🟡",
}


async def send_telegram_alert(message: str, severity: str) -> None:
    """Queue a Telegram alert and return immediately.

    `severity`: one of 'critical', 'warning', 'info' (case-insensitive).
    'info' is dashboard-only — Telegram is NOT contacted.
    """
    sev = severity.lower()
    if sev not in _PREFIX_BY_SEVERITY:
        # 'info' or anything else → not a Telegram alert.
        return

    if settings.telegram_demo_mode:
        logger.info("[DEMO] Telegram alert would be sent: %s", message)
        return

    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning(
            "Telegram alert skipped — token or chat_id not configured (severity=%s)",
            sev,
        )
        return

    prefix = _PREFIX_BY_SEVERITY[sev]
    text = f"{prefix} {sev.upper()}: {message}"

    try:
        asyncio.get_running_loop()
        asyncio.create_task(_post_to_telegram(text))
    except RuntimeError:
        # No running loop (sync caller in tests). Fall back to a one-shot run
        # so the HTTP attempt still happens.
        asyncio.run(_post_to_telegram(text))


async def _post_to_telegram(text: str) -> None:
    url = f"{TELEGRAM_API_BASE}/bot{settings.telegram_bot_token}/sendMessage"
    payload = {"chat_id": settings.telegram_chat_id, "text": text}
    try:
        async with httpx.AsyncClient(timeout=TELEGRAM_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        logger.info("Telegram OK: %s", text[:120])
    except httpx.HTTPStatusError as exc:
        logger.error(
            "Telegram HTTP %s: %s",
            exc.response.status_code,
            exc.response.text[:300],
        )
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)
