"""System alert creation — BILLING_LOGIC.md §14.1.

Inserts an alerts row and dispatches notifications based on severity:
- critical → Telegram + email
- warning  → Telegram only
- info     → dashboard only (no Telegram, no email)

Callers pass their AsyncSession. We `flush()` so the row gets an id but never
`commit()` — the surrounding pipeline owns the transaction. Telegram dispatch
is fire-and-forget; the HTTP attempt runs on a background task that can outlive
this function's return. The critical-alert email is best-effort: a failure is
logged and swallowed so it never breaks the surrounding alert/billing pipeline.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert
from app.services.email_service import send_alert_email
from app.services.telegram_service import send_telegram_alert

logger = logging.getLogger(__name__)


async def create_alert(
    db: AsyncSession,
    severity: str,
    source: str,
    title: str,
    message: str | None = None,
    details: dict[str, Any] | None = None,
) -> Alert:
    """Create an alert and dispatch notifications by severity.

    Returns the inserted Alert (flushed, not committed). The caller commits.
    `severity` must be 'critical', 'warning', or 'info' to satisfy the
    alert_severity enum on the alerts table; other values will fail at flush.
    """
    alert = Alert(
        severity=severity,
        status="new",
        title=title,
        message=message or title,
        source=source,
        details=details,
    )
    db.add(alert)
    await db.flush()

    sev = severity.lower()
    if sev == "critical":
        await send_telegram_alert(title, severity="critical")
        try:
            await send_alert_email(title, message or title)
        except Exception:
            logger.exception("send_alert_email failed for alert %r", title)
    elif sev == "warning":
        await send_telegram_alert(title, severity="warning")
    # 'info' → dashboard only (intentional fall-through).

    return alert
