"""Tests for the admin alerts + audit log endpoints (Task 8.5)."""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert
from app.models.audit_log import AdminAuditLog
from app.models.transaction import Transaction
from app.models.user import User

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
ALERTS_URL = "/api/v1/admin/alerts"
AUDIT_URL = "/api/v1/admin/audit"


def _ack_url(aid: str) -> str:
    return f"/api/v1/admin/alerts/{aid}/acknowledge"


def _resolve_url(aid: str) -> str:
    return f"/api/v1/admin/alerts/{aid}/resolve"


_TEST_EMAILS = ["t85_admin@example.com", "t85_other@example.com"]
# Unique source tag so my fixtures only touch this file's alert rows even when
# leftover alerts from other test files exist in the shared DB.
_ALERT_SOURCE = "test_admin_alerts_audit"


@pytest_asyncio.fixture(autouse=True)
async def clean_state(db_session: AsyncSession) -> None:
    # Wipe only the rows this file created — alerts by source, audit log /
    # transactions by actor / admin.
    await db_session.execute(
        sa_delete(Alert).where(Alert.source == _ALERT_SOURCE)
    )
    for email in _TEST_EMAILS:
        existing = (
            await db_session.execute(
                select(User).where(User.email_lower == email.lower())
            )
        ).scalar_one_or_none()
        if existing is not None:
            await db_session.execute(
                sa_delete(Transaction).where(
                    Transaction.admin_user_id == existing.id
                )
            )
            await db_session.execute(
                sa_delete(AdminAuditLog).where(
                    AdminAuditLog.actor_id == existing.id
                )
            )
            await db_session.delete(existing)
    await db_session.commit()


async def _register_admin(
    http: AsyncClient, db: AsyncSession, email: str
) -> tuple[str, str]:
    r = await http.post(
        REGISTER_URL,
        json={"email": email, "password": "SecurePass1", "display_name": "Имя"},
    )
    assert r.status_code == 200, r.text
    uid = r.json()["user"]["id"]
    user = (
        await db.execute(select(User).where(User.id == _uuid.UUID(uid)))
    ).scalar_one()
    user.role = "admin"
    await db.commit()
    login = await http.post(
        LOGIN_URL, json={"email": email, "password": "SecurePass1"}
    )
    assert login.status_code == 200
    return login.json()["access_token"], uid


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_alert(
    db: AsyncSession,
    *,
    severity: str = "warning",
    status: str = "new",
    title: str = "Test alert",
    message: str = "Synthetic alert for tests",
) -> Alert:
    alert = Alert(
        severity=severity,
        status=status,
        title=title,
        message=message,
        source=_ALERT_SOURCE,
        details={"k": "v"},
    )
    db.add(alert)
    await db.flush()
    return alert


# ── Alerts tests ─────────────────────────────────────────────────────────────


async def test_list_alerts_unread_count(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "t85_admin@example.com"
    )

    # Capture baseline unread count from any leftovers in the shared DB so we
    # can assert relative to it.
    baseline = await http_client.get(
        ALERTS_URL, headers=_auth(admin_token), params={"per_page": 1}
    )
    baseline_unread = baseline.json()["unread_count"]

    for _ in range(3):
        await _seed_alert(db_session, status="new")
    await _seed_alert(db_session, status="resolved")
    await db_session.commit()

    resp = await http_client.get(
        ALERTS_URL, headers=_auth(admin_token), params={"per_page": 50}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["unread_count"] == baseline_unread + 3


async def test_acknowledge_alert(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "t85_admin@example.com"
    )
    alert = await _seed_alert(db_session, status="new")
    await db_session.commit()

    resp = await http_client.post(
        _ack_url(str(alert.id)), headers=_auth(admin_token)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "acknowledged"
    assert data["acknowledged_at"] is not None
    assert data["acknowledged_by"] is not None


async def test_resolve_alert(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "t85_admin@example.com"
    )
    alert = await _seed_alert(db_session, status="new")
    await db_session.commit()

    resp = await http_client.post(
        _resolve_url(str(alert.id)), headers=_auth(admin_token)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "resolved"
    assert data["resolved_at"] is not None


async def test_alert_severity_filter(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "t85_admin@example.com"
    )
    await _seed_alert(db_session, severity="critical")
    await _seed_alert(db_session, severity="warning")
    await db_session.commit()

    resp = await http_client.get(
        ALERTS_URL,
        headers=_auth(admin_token),
        params={"severity": "critical", "per_page": 100},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    # All returned items must be critical; at least one of ours is present.
    mine = [i for i in items if i["source"] == _ALERT_SOURCE]
    assert len(mine) == 1
    assert all(i["severity"] == "critical" for i in items)


# ── Audit tests ──────────────────────────────────────────────────────────────


async def test_audit_log_search_by_action(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, admin_id = await _register_admin(
        http_client, db_session, "t85_admin@example.com"
    )
    # Distinct actions for this admin.
    db_session.add(
        AdminAuditLog(
            actor_id=_uuid.UUID(admin_id),
            actor_email="t85_admin@example.com",
            action="test85.synth.alpha",
            target_type="test",
            target_id="x",
        )
    )
    db_session.add(
        AdminAuditLog(
            actor_id=_uuid.UUID(admin_id),
            actor_email="t85_admin@example.com",
            action="test85.synth.beta",
            target_type="test",
            target_id="y",
        )
    )
    await db_session.commit()

    resp = await http_client.get(
        AUDIT_URL,
        headers=_auth(admin_token),
        params={
            "action": "test85.synth.alpha",
            "actor_id": admin_id,
            "per_page": 50,
        },
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["action"] == "test85.synth.alpha"


async def test_audit_log_date_filter(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, admin_id = await _register_admin(
        http_client, db_session, "t85_admin@example.com"
    )
    now = datetime.now(timezone.utc)
    today = now.replace(hour=12, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)

    db_session.add(
        AdminAuditLog(
            actor_id=_uuid.UUID(admin_id),
            actor_email="t85_admin@example.com",
            action="test85.date.today",
            target_type="test",
            target_id="today",
            created_at=today,
        )
    )
    db_session.add(
        AdminAuditLog(
            actor_id=_uuid.UUID(admin_id),
            actor_email="t85_admin@example.com",
            action="test85.date.yesterday",
            target_type="test",
            target_id="yesterday",
            created_at=yesterday,
        )
    )
    await db_session.commit()

    # date_from = today → exclude yesterday's row.
    resp = await http_client.get(
        AUDIT_URL,
        headers=_auth(admin_token),
        params={
            "action": "test85.date",
            "actor_id": admin_id,
            "date_from": now.date().isoformat(),
            "per_page": 50,
        },
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    actions = {i["action"] for i in items}
    assert "test85.date.today" in actions
    assert "test85.date.yesterday" not in actions
