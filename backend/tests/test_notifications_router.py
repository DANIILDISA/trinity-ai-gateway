"""Tests for the in-app notification endpoints (SPEC.md API table).

These endpoints live on the users router under /api/v1/users/me/notifications
(GET list, POST {id}/read). There is no standalone /notifications router — the
empty api/v1/notifications.py scaffolding stub was removed. SPEC defines no
DELETE/dismiss endpoint, so none is exercised here.
"""
import uuid as _uuid

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AdminAuditLog
from app.models.notification import Notification
from app.models.transaction import Transaction
from app.models.user import User

REGISTER_URL = "/api/v1/auth/register"
LIST_URL = "/api/v1/users/me/notifications"

_TEST_EMAILS = [
    "test_notif_list@example.com",
    "test_notif_read@example.com",
    "test_notif_isolation@example.com",
]

_BASE_PAYLOAD = {"password": "SecurePass1", "display_name": "Тест"}


@pytest_asyncio.fixture(autouse=True)
async def clean_test_users(db_session: AsyncSession) -> None:
    for email in _TEST_EMAILS:
        existing = (
            await db_session.execute(
                select(User).where(User.email_lower == email.lower())
            )
        ).scalar_one_or_none()
        if existing is not None:
            await db_session.execute(
                sa_delete(Notification).where(Notification.user_id == existing.id)
            )
            await db_session.execute(
                sa_delete(Transaction).where(Transaction.user_id == existing.id)
            )
            await db_session.execute(
                sa_delete(AdminAuditLog).where(AdminAuditLog.actor_id == existing.id)
            )
            await db_session.delete(existing)
    await db_session.commit()


async def _register(http_client: AsyncClient, email: str) -> tuple[str, str]:
    resp = await http_client.post(REGISTER_URL, json={**_BASE_PAYLOAD, "email": email})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return data["access_token"], data["user"]["id"]


async def _seed_notification(
    db_session: AsyncSession,
    user_id: str,
    *,
    type: str = "low_balance",
    title: str = "Тест",
    message: str = "Тестовое уведомление",
    is_read: bool = False,
) -> _uuid.UUID:
    note = Notification(
        user_id=_uuid.UUID(user_id),
        type=type,
        title=title,
        message=message,
        channel="in_app",
        is_read=is_read,
    )
    db_session.add(note)
    await db_session.commit()
    return note.id


# ── GET list ────────────────────────────────────────────────────────────────


async def test_list_notifications_returns_user_notifications(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, user_id = await _register(http_client, "test_notif_list@example.com")
    await _seed_notification(
        db_session, user_id, title="Баланс заканчивается", message="Осталось мало"
    )

    resp = await http_client.get(
        LIST_URL, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["page"] == 1
    assert data["per_page"] == 20
    assert data["pages"] == 1
    assert len(data["items"]) == 1

    item = data["items"][0]
    assert item["type"] == "low_balance"
    assert item["title"] == "Баланс заканчивается"
    assert item["message"] == "Осталось мало"
    assert item["channel"] == "in_app"
    assert item["is_read"] is False
    assert "id" in item
    assert "created_at" in item


async def test_list_notifications_only_returns_own(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    # Owner has a notification; a second user must not see it.
    _, owner_id = await _register(http_client, "test_notif_isolation@example.com")
    await _seed_notification(db_session, owner_id, title="Приватное")

    other_token, _ = await _register(http_client, "test_notif_list@example.com")
    resp = await http_client.get(
        LIST_URL, headers={"Authorization": f"Bearer {other_token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


# ── POST {id}/read ───────────────────────────────────────────────────────────


async def test_mark_notification_read(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, user_id = await _register(http_client, "test_notif_read@example.com")
    note_id = await _seed_notification(db_session, user_id, is_read=False)

    resp = await http_client.post(
        f"{LIST_URL}/{note_id}/read",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 204

    db_session.expire_all()
    note = (
        await db_session.execute(
            select(Notification).where(Notification.id == note_id)
        )
    ).scalar_one()
    assert note.is_read is True


async def test_mark_nonexistent_notification_returns_404(
    http_client: AsyncClient,
) -> None:
    token, _ = await _register(http_client, "test_notif_read@example.com")
    resp = await http_client.post(
        f"{LIST_URL}/{_uuid.uuid4()}/read",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_cannot_mark_other_users_notification(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    # A notification owned by user A is invisible (404) to user B's mark-read.
    _, owner_id = await _register(http_client, "test_notif_isolation@example.com")
    note_id = await _seed_notification(db_session, owner_id)

    other_token, _ = await _register(http_client, "test_notif_read@example.com")
    resp = await http_client.post(
        f"{LIST_URL}/{note_id}/read",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 404


# ── Auth gating ───────────────────────────────────────────────────────────────


async def test_list_unauthenticated_returns_401(http_client: AsyncClient) -> None:
    resp = await http_client.get(
        LIST_URL, headers={"Authorization": "Bearer invalid.token.value"}
    )
    assert resp.status_code == 401


async def test_mark_read_unauthenticated_returns_401(
    http_client: AsyncClient,
) -> None:
    resp = await http_client.post(
        f"{LIST_URL}/{_uuid.uuid4()}/read",
        headers={"Authorization": "Bearer invalid.token.value"},
    )
    assert resp.status_code == 401
