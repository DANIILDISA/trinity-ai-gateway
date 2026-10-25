"""Tests for the user notification service (Task 9.2)."""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.audit_log import AdminAuditLog
from app.models.notification import Notification
from app.models.refresh_token import RefreshToken
from app.models.reservation import Reservation
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services import email_service, notification_service

REGISTER_URL = "/api/v1/auth/register"
NOTIFICATIONS_URL = "/api/v1/users/me/notifications"

_TEST_EMAILS = [
    "notif_low@example.com",
    "notif_low_dedup@example.com",
    "notif_low_expire@example.com",
    "notif_topup@example.com",
    "notif_tariff_a@example.com",
    "notif_tariff_b@example.com",
    "notif_list@example.com",
    "notif_read@example.com",
    "notif_read_wrong@example.com",
]


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(autouse=True)
async def _silence_email(monkeypatch: pytest.MonkeyPatch):
    """Email helpers are fire-and-forget by design; in tests we want them to
    be true no-ops so no Resend HTTP call ever escapes the test process."""
    monkeypatch.setattr(email_service.settings, "email_demo_mode", True)


async def _purge_users(db: AsyncSession) -> None:
    # tariff_change fan-out creates a row for EVERY active user, not just the
    # two this file creates. Wipe all tariff_change rows so we don't leave
    # behind FK obstacles for other test files' cleanup fixtures (test_auth,
    # test_conversations, etc. don't know about notifications).
    await db.execute(
        sa_delete(Notification).where(Notification.type == "tariff_change")
    )
    for email in _TEST_EMAILS:
        existing = (
            await db.execute(
                select(User).where(User.email_lower == email.lower())
            )
        ).scalar_one_or_none()
        if existing is None:
            continue
        uid = existing.id
        await db.execute(sa_delete(Notification).where(Notification.user_id == uid))
        await db.execute(sa_delete(UsageLog).where(UsageLog.user_id == uid))
        await db.execute(sa_delete(Reservation).where(Reservation.user_id == uid))
        await db.execute(sa_delete(Transaction).where(Transaction.user_id == uid))
        await db.execute(sa_delete(RefreshToken).where(RefreshToken.user_id == uid))
        await db.execute(
            sa_delete(AdminAuditLog).where(AdminAuditLog.actor_id == uid)
        )
        await db.delete(existing)
    await db.commit()


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(db_session: AsyncSession):
    await _purge_users(db_session)
    yield
    await _purge_users(db_session)


async def _create_user(
    db: AsyncSession, email: str, balance_micro_rub: int = 0
) -> User:
    # email_lower is a generated column — set by the DB, not the ORM.
    user = User(
        email=email,
        password_hash=await hash_password("Password1"),
        display_name="Тестовый",
        role="user",
        status="active",
        balance_micro_rub=balance_micro_rub,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _seed_completed_request(
    db: AsyncSession, user_id, cost_micro_rub: int
) -> None:
    """Insert a synthetic completed usage_log so the avg-cost lookup has data."""
    db.add(
        UsageLog(
            user_id=user_id,
            request_id=_uuid.uuid4(),
            model_id="openai/gpt-4o",
            input_tokens=100,
            output_tokens=100,
            estimated_input_tokens=100,
            estimated_output_tokens=100,
            estimated_cost_micro_rub=cost_micro_rub,
            actual_cost_micro_rub=cost_micro_rub,
            reserved_micro_rub=cost_micro_rub * 2,
            overdraft_absorbed_micro_rub=0,
            openrouter_cost_micro_usd=0,
            exchange_rate_micro_rub=0,
            status="completed",
        )
    )
    await db.commit()


async def _register(http_client: AsyncClient, email: str) -> tuple[str, str]:
    resp = await http_client.post(
        REGISTER_URL,
        json={
            "email": email,
            "password": "Password1",
            "display_name": "Тестовый",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return data["access_token"], data["user"]["id"]


# ── Low balance ─────────────────────────────────────────────────────────────


async def test_low_balance_creates_notification(
    db_session: AsyncSession,
) -> None:
    user = await _create_user(db_session, "notif_low@example.com")
    # avg cost = 1_000_000 μ₽ (1 ₽); balance covers exactly 3 requests, threshold = 5.
    await _seed_completed_request(db_session, user.id, cost_micro_rub=1_000_000)
    await notification_service.check_low_balance_notification(
        db_session, user.id, current_balance_micro_rub=3_000_000
    )
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(Notification).where(Notification.user_id == user.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    note = rows[0]
    assert note.type == "low_balance"
    assert note.title == "Баланс заканчивается"
    assert "3" in note.message  # remaining_requests
    assert note.channel == "both"


async def test_low_balance_no_notification_above_threshold(
    db_session: AsyncSession,
) -> None:
    user = await _create_user(db_session, "notif_low_expire@example.com")
    # 100 ₽ balance / 1 ₽ avg = 100 remaining > threshold 5 → no notification.
    await _seed_completed_request(db_session, user.id, cost_micro_rub=1_000_000)
    await notification_service.check_low_balance_notification(
        db_session, user.id, current_balance_micro_rub=100_000_000
    )
    await db_session.commit()

    count = (
        await db_session.execute(
            select(Notification).where(Notification.user_id == user.id)
        )
    ).scalars().all()
    assert count == []


async def test_low_balance_dedup_within_24h(
    db_session: AsyncSession,
) -> None:
    user = await _create_user(db_session, "notif_low_dedup@example.com")
    await _seed_completed_request(db_session, user.id, cost_micro_rub=1_000_000)

    await notification_service.check_low_balance_notification(
        db_session, user.id, current_balance_micro_rub=2_000_000
    )
    await db_session.commit()
    await notification_service.check_low_balance_notification(
        db_session, user.id, current_balance_micro_rub=2_000_000
    )
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(Notification).where(
                Notification.user_id == user.id,
                Notification.type == "low_balance",
            )
        )
    ).scalars().all()
    assert len(rows) == 1, "second call within 24h must NOT create a 2nd row"


async def test_low_balance_creates_again_after_24h(
    db_session: AsyncSession,
) -> None:
    user = await _create_user(db_session, "notif_low_expire@example.com")
    await _seed_completed_request(db_session, user.id, cost_micro_rub=1_000_000)

    await notification_service.check_low_balance_notification(
        db_session, user.id, current_balance_micro_rub=2_000_000
    )
    await db_session.commit()

    # Backdate the existing notification to 25 hours ago.
    existing = (
        await db_session.execute(
            select(Notification).where(
                Notification.user_id == user.id,
                Notification.type == "low_balance",
            )
        )
    ).scalar_one()
    existing.created_at = datetime.now(timezone.utc) - timedelta(hours=25)
    await db_session.commit()

    await notification_service.check_low_balance_notification(
        db_session, user.id, current_balance_micro_rub=2_000_000
    )
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(Notification).where(
                Notification.user_id == user.id,
                Notification.type == "low_balance",
            )
        )
    ).scalars().all()
    assert len(rows) == 2, "after 24h+ a new notification should be created"


# ── Top-up ──────────────────────────────────────────────────────────────────


async def test_topup_notification_created(db_session: AsyncSession) -> None:
    user = await _create_user(db_session, "notif_topup@example.com")
    await notification_service.create_topup_notification(
        db_session,
        user.id,
        amount_micro_rub=500_000_000,  # 500 ₽
        new_balance_micro_rub=750_000_000,  # 750 ₽
    )
    await db_session.commit()

    note = (
        await db_session.execute(
            select(Notification).where(Notification.user_id == user.id)
        )
    ).scalar_one()
    assert note.type == "topup_confirmed"
    assert note.title == "Баланс пополнен"
    assert "500.00" in note.message
    assert "750.00" in note.message
    assert note.channel == "in_app"


# ── Tariff change fan-out ───────────────────────────────────────────────────


async def test_tariff_change_notifies_all_active_users(
    db_session: AsyncSession,
) -> None:
    u_a = await _create_user(db_session, "notif_tariff_a@example.com")
    u_b = await _create_user(db_session, "notif_tariff_b@example.com")

    await notification_service.create_tariff_change_notifications(
        db_session,
        old_rate_micro_rub=80_000_000,  # 80 ₽/USD
        new_rate_micro_rub=90_000_000,  # 90 ₽/USD
    )
    await db_session.commit()

    notes_a = (
        await db_session.execute(
            select(Notification).where(
                Notification.user_id == u_a.id,
                Notification.type == "tariff_change",
            )
        )
    ).scalars().all()
    notes_b = (
        await db_session.execute(
            select(Notification).where(
                Notification.user_id == u_b.id,
                Notification.type == "tariff_change",
            )
        )
    ).scalars().all()
    assert len(notes_a) == 1
    assert len(notes_b) == 1
    assert notes_a[0].title == "Обновление тарифов"
    assert "увеличены" in notes_a[0].message  # new > old
    # BUG 18: tariff changes are in-app only — no email channel.
    assert notes_a[0].channel == "in_app"


# ── Endpoints ───────────────────────────────────────────────────────────────


async def test_list_notifications_paginated(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, user_id = await _register(http_client, "notif_list@example.com")
    uid = _uuid.UUID(user_id)
    # Seed three notifications with explicit, staggered created_at so the
    # DESC ordering assertion below is deterministic.
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    for i in range(3):
        db_session.add(
            Notification(
                user_id=uid,
                type="low_balance",
                title=f"Заголовок {i}",
                message=f"Сообщение {i}",
                channel="in_app",
                created_at=base + timedelta(minutes=i),
            )
        )
    await db_session.commit()

    resp = await http_client.get(
        NOTIFICATIONS_URL,
        headers={"Authorization": f"Bearer {token}"},
        params={"page": 1, "per_page": 2},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert body["pages"] == 2
    assert body["page"] == 1
    assert body["per_page"] == 2
    assert len(body["items"]) == 2
    # DESC ordering — most recent first.
    assert body["items"][0]["title"] == "Заголовок 2"


async def test_mark_notification_read_success(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, user_id = await _register(http_client, "notif_read@example.com")
    note = Notification(
        user_id=_uuid.UUID(user_id),
        type="topup_confirmed",
        title="Баланс пополнен",
        message="100.00",
        channel="in_app",
    )
    db_session.add(note)
    await db_session.commit()
    await db_session.refresh(note)

    resp = await http_client.post(
        f"{NOTIFICATIONS_URL}/{note.id}/read",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 204

    await db_session.refresh(note)
    assert note.is_read is True


async def test_mark_notification_read_wrong_user_returns_404(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    # User A owns the notification; user B tries to mark it read.
    _, owner_id = await _register(http_client, "notif_read@example.com")
    intruder_token, _ = await _register(http_client, "notif_read_wrong@example.com")

    note = Notification(
        user_id=_uuid.UUID(owner_id),
        type="low_balance",
        title="Чужое",
        message="Чужое",
        channel="in_app",
    )
    db_session.add(note)
    await db_session.commit()
    await db_session.refresh(note)

    resp = await http_client.post(
        f"{NOTIFICATIONS_URL}/{note.id}/read",
        headers={"Authorization": f"Bearer {intruder_token}"},
    )
    assert resp.status_code == 404

    await db_session.refresh(note)
    assert note.is_read is False  # untouched


async def test_mark_notification_read_unknown_id_returns_404(
    http_client: AsyncClient,
) -> None:
    token, _ = await _register(http_client, "notif_read@example.com")
    resp = await http_client.post(
        f"{NOTIFICATIONS_URL}/{_uuid.uuid4()}/read",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
