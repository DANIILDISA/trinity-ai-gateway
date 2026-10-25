"""Email service backed by Resend HTTP API + Jinja2 templates.

Two public surfaces:

1. **Typed module-level helpers** — the current API:
   - send_password_reset(to_email, reset_link)
   - send_low_balance(to_email, remaining_requests, topup_link)
   - send_topup_confirmed(to_email, amount, new_balance)
   - send_tariff_change(to_email, old_tariff, new_tariff)

   All four render Jinja2 (HTML + plain-text), then queue the Resend HTTP call
   with asyncio.create_task — callers don't block on the network. Failures are
   logged, never raised.

2. **EmailServiceProtocol / get_email_service()** — legacy DI used by
   request_password_reset in auth_service. Kept so existing tests work without
   changes. Production implementation routes through the same Resend helpers.

In DEMO_MODE the actual HTTP call is skipped and a single line is logged.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Protocol

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import settings

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"
RESEND_TIMEOUT_SECONDS = 10.0

# Cap concurrent Resend network calls (audit M8). Sends are fire-and-forget
# (asyncio.create_task), so a burst — mass low-balance / topup notifications —
# could otherwise fan out unbounded concurrent httpx clients/sockets. Excess
# sends queue on the semaphore instead of all hitting the network at once. Python
# 3.10+ asyncio primitives bind to the running loop lazily, so a module-level
# Semaphore is safe.
_EMAIL_MAX_CONCURRENCY = 20
_EMAIL_SEMAPHORE = asyncio.Semaphore(_EMAIL_MAX_CONCURRENCY)

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "email"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
    enable_async=False,
)


# ── Public typed helpers ────────────────────────────────────────────────────


async def send_password_reset(to_email: str, reset_link: str) -> None:
    subject = "Сброс пароля Trinity"
    html = _render("password_reset.html", reset_link=reset_link)
    text = _render("password_reset.txt", reset_link=reset_link)
    _dispatch(to_email, subject, text, html)


async def send_low_balance(
    to_email: str, remaining_requests: int, topup_link: str
) -> None:
    subject = "Низкий баланс Trinity"
    ctx = {"remaining_requests": remaining_requests, "topup_link": topup_link}
    html = _render("low_balance.html", **ctx)
    text = _render("low_balance.txt", **ctx)
    _dispatch(to_email, subject, text, html)


async def send_topup_confirmed(
    to_email: str, amount: float, new_balance: float
) -> None:
    subject = "Пополнение баланса Trinity"
    ctx = {"amount": amount, "new_balance": new_balance}
    html = _render("topup_confirmed.html", **ctx)
    text = _render("topup_confirmed.txt", **ctx)
    _dispatch(to_email, subject, text, html)


async def send_tariff_change(
    to_email: str, old_tariff: str, new_tariff: str
) -> None:
    subject = "Изменение тарифов Trinity"
    ctx = {"old_tariff": old_tariff, "new_tariff": new_tariff}
    html = _render("tariff_change.html", **ctx)
    text = _render("tariff_change.txt", **ctx)
    _dispatch(to_email, subject, text, html)


async def send_alert_email(title: str, message: str) -> None:
    """Admin notification for critical alerts (BILLING_LOGIC.md §14.1).

    Sends to `settings.admin_email`. Skips silently with a warning if no
    admin email is configured — the alert is already in the `alerts` table
    and on Telegram, so a missing alert email never breaks the caller.
    """
    if not settings.admin_email:
        logger.warning(
            "send_alert_email skipped — admin_email not configured (title=%s)",
            title,
        )
        return
    subject = f"[Trinity ALERT] {title}"
    ctx = {"title": title, "message": message}
    html = _render("alert.html", **ctx)
    text = _render("alert.txt", **ctx)
    _dispatch(settings.admin_email, subject, text, html)


# ── Internals ───────────────────────────────────────────────────────────────


def _render(template_name: str, **vars: Any) -> str:
    return _env.get_template(template_name).render(**vars)


def _dispatch(to: str, subject: str, text: str, html: str) -> None:
    """Queue Resend send without awaiting it. EMAIL_DEMO_MODE short-circuits."""
    if settings.email_demo_mode:
        logger.info("[DEMO] Email would be sent to %s: %s", to, subject)
        return
    try:
        asyncio.get_running_loop()
        asyncio.create_task(_resend_send(to, subject, text, html))
    except RuntimeError:
        # No running loop (e.g. called from a sync context in tests). Fall back
        # to a synchronous one-shot run so the network attempt still happens.
        asyncio.run(_resend_send(to, subject, text, html))


async def _resend_send(to: str, subject: str, text: str, html: str) -> None:
    if not settings.resend_api_key:
        logger.error("RESEND_API_KEY not configured; dropping email to %s", to)
        return
    payload = {
        "from": settings.resend_from_email,
        "to": [to],
        "subject": subject,
        "text": text,
        "html": html,
    }
    headers = {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
    }
    try:
        # Bound concurrent network sends (audit M8).
        async with _EMAIL_SEMAPHORE:
            async with httpx.AsyncClient(timeout=RESEND_TIMEOUT_SECONDS) as client:
                resp = await client.post(RESEND_ENDPOINT, json=payload, headers=headers)
                resp.raise_for_status()
        logger.info("Resend OK: to=%s subject=%s", to, subject)
    except httpx.HTTPStatusError as exc:
        logger.error(
            "Resend HTTP %s for to=%s: %s",
            exc.response.status_code,
            to,
            exc.response.text[:300],
        )
    except Exception as exc:
        logger.error("Resend failed for to=%s: %s", to, exc)


# ── Legacy DI surface (used by auth_service.request_password_reset) ─────────


class EmailServiceProtocol(Protocol):
    async def send_email(self, to: str, subject: str, body: str) -> None: ...


class ResendEmailService:
    """Production implementation — routes plain-text email through Resend."""

    async def send_email(self, to: str, subject: str, body: str) -> None:
        if settings.email_demo_mode:
            logger.info("[DEMO] Email would be sent to %s: %s", to, subject)
            return
        try:
            asyncio.get_running_loop()
            asyncio.create_task(_resend_send(to, subject, body, body))
        except RuntimeError:
            asyncio.run(_resend_send(to, subject, body, body))


class DevelopmentEmailService:
    async def send_email(self, to: str, subject: str, body: str) -> None:
        logger.info("[EMAIL] To: %s | Subject: %s | Body: %s", to, subject, body)


class MockEmailService:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send_email(self, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})


def get_email_service() -> EmailServiceProtocol:
    # EMAIL_DEMO_MODE is the single switch: when false and a Resend key is
    # configured, send real mail. The previous `app_env == "production"`
    # gate disagreed with the typed helpers (send_password_reset etc., which
    # only check EMAIL_DEMO_MODE) and silently dropped password-reset emails
    # in any non-production environment.
    if settings.email_demo_mode:
        return DevelopmentEmailService()
    if settings.resend_api_key:
        return ResendEmailService()
    return DevelopmentEmailService()
