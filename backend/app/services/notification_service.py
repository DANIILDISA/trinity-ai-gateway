"""User-facing notifications — BILLING_LOGIC.md §15.

In-app rows go into the `notifications` table; email goes through Resend
helpers in email_service. Email is always fire-and-forget — callers do not
await delivery, and a failed send never raises.

All three entry points accept the caller's AsyncSession and write through it
without committing — the surrounding pipeline owns the transaction.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.constants import MICRO_PER_UNIT, micro_to_display_balance
from app.models.notification import Notification
from app.models.system_setting import SystemSetting
from app.models.user import User
from app.services import email_service

logger = logging.getLogger(__name__)

LOW_BALANCE_DEDUP_HOURS = 24
LOW_BALANCE_DEFAULT_THRESHOLD = 5
MIN_TOPUP_DEFAULT_RUB = 500


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _get_setting_int(db: AsyncSession, key: str, default: int) -> int:
    """Read an int from system_settings.value (stored as JSONB)."""
    row = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    ).scalar_one_or_none()
    if row is None or row.value is None:
        return default
    try:
        return int(row.value)
    except (TypeError, ValueError):
        return default


async def _get_avg_request_cost_micro_rub(
    db: AsyncSession, user_id: uuid.UUID
) -> int | None:
    """Average actual_cost over the user's last 100 completed requests.
    Returns None if the user has no completed-request history."""
    row = await db.execute(
        text(
            """
            SELECT AVG(actual_cost_micro_rub)::BIGINT AS avg_cost
            FROM (
                SELECT actual_cost_micro_rub
                FROM usage_logs
                WHERE user_id = :uid AND status = 'completed'
                ORDER BY created_at DESC
                LIMIT 100
            ) sub
            """
        ),
        {"uid": user_id},
    )
    avg = row.scalar()
    if avg is None or avg <= 0:
        return None
    return int(avg)


# ── Low balance ─────────────────────────────────────────────────────────────


async def check_low_balance_notification(
    db: AsyncSession,
    user_id: uuid.UUID,
    current_balance_micro_rub: int,
) -> None:
    """Called after every successful finalization. BILLING_LOGIC.md §15.1.

    Creates an in-app notification and fires a `send_low_balance` email when
    `remaining_requests <= threshold`. Suppressed if a low_balance notification
    was already created for this user in the last 24 hours.
    """
    threshold = await _get_setting_int(
        db, "low_balance_remaining_requests", LOW_BALANCE_DEFAULT_THRESHOLD
    )

    avg_cost = await _get_avg_request_cost_micro_rub(db, user_id)
    if avg_cost is None:
        return  # New user, no history.

    remaining_requests = current_balance_micro_rub // avg_cost
    if remaining_requests > threshold:
        return

    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOW_BALANCE_DEDUP_HOURS)
    existing = (
        await db.execute(
            select(Notification.id)
            .where(
                Notification.user_id == user_id,
                Notification.type == "low_balance",
                Notification.created_at > cutoff,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return

    min_topup = await _get_setting_int(
        db, "min_topup_amount_rub", MIN_TOPUP_DEFAULT_RUB
    )
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        return

    db.add(
        Notification(
            user_id=user_id,
            type="low_balance",
            title="Баланс заканчивается",
            message=(
                f"На вашем балансе осталось средств примерно на "
                f"{remaining_requests} запрос(ов). "
                f"Минимальная сумма пополнения: {min_topup}₽."
            ),
            channel="both",
        )
    )
    await db.flush()

    topup_link = f"{settings.frontend_url}/balance"
    # Fire-and-forget — send_low_balance returns as soon as the task is queued.
    await email_service.send_low_balance(user.email, remaining_requests, topup_link)


# ── Top-up confirmation ─────────────────────────────────────────────────────


async def create_topup_notification(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount_micro_rub: int,
    new_balance_micro_rub: int,
) -> None:
    """In-app + email after a successful YooKassa top-up. BILLING_LOGIC.md §15.2."""
    amount_display = micro_to_display_balance(amount_micro_rub)
    balance_display = micro_to_display_balance(new_balance_micro_rub)

    db.add(
        Notification(
            user_id=user_id,
            type="topup_confirmed",
            # Title kept short to match the form asserted by test_payments.py;
            # the amount lives in the message.
            title="Баланс пополнен",
            message=(
                f"Ваш баланс пополнен на {amount_display}₽. "
                f"Текущий баланс: {balance_display}₽."
            ),
            channel="in_app",
        )
    )
    await db.flush()

    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        return

    await email_service.send_topup_confirmed(
        user.email,
        amount=amount_micro_rub / MICRO_PER_UNIT,
        new_balance=new_balance_micro_rub / MICRO_PER_UNIT,
    )


# ── Tariff change ──────────────────────────────────────────────────────────


async def create_tariff_change_notifications(
    db: AsyncSession,
    old_rate_micro_rub: int | None,
    new_rate_micro_rub: int,
) -> None:
    """Fan-out to every active user after an exchange-rate change.
    BILLING_LOGIC.md §15.3."""
    direction = (
        "увеличены"
        if old_rate_micro_rub is not None and new_rate_micro_rub > old_rate_micro_rub
        else "снижены"
    )
    message = (
        f"Тарифы на использование моделей были {direction}. "
        f"Актуальные цены доступны на странице тарифов."
    )

    users = (
        await db.execute(select(User).where(User.status == "active"))
    ).scalars().all()

    for user in users:
        db.add(
            Notification(
                user_id=user.id,
                type="tariff_change",
                title="Обновление тарифов",
                message=message,
                # In-app only (BUG 18): tariff changes no longer trigger email.
                channel="in_app",
            )
        )
    await db.flush()
    # NOTE: email intentionally NOT sent for tariff changes (BUG 18). The
    # send_tariff_change template remains in email_service for other callers.
