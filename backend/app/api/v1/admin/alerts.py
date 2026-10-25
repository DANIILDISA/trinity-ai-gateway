"""Admin alerts endpoints (Task 8.5).

GET    /api/v1/admin/alerts                    — list with filters + unread_count
POST   /api/v1/admin/alerts/{id}/acknowledge   — flip new → acknowledged
POST   /api/v1/admin/alerts/{id}/resolve       — flip any → resolved
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_admin
from app.models.alert import Alert
from app.models.audit_log import AdminAuditLog
from app.models.user import User

router = APIRouter()

AlertStatus = Literal["new", "acknowledged", "resolved"]
AlertSeverity = Literal["critical", "warning", "info"]


# ── Schemas ─────────────────────────────────────────────────────────────────


class AlertItem(BaseModel):
    id: uuid.UUID
    severity: str
    status: str
    title: str
    message: str
    source: str
    details: dict[str, Any] | None
    created_at: datetime
    acknowledged_at: datetime | None
    acknowledged_by: uuid.UUID | None
    resolved_at: datetime | None
    resolved_by: uuid.UUID | None


class AlertListResponse(BaseModel):
    items: list[AlertItem]
    total: int
    page: int
    per_page: int
    unread_count: int


# ── Helpers ─────────────────────────────────────────────────────────────────


def _to_item(row: Alert) -> AlertItem:
    return AlertItem(
        id=row.id,
        severity=row.severity,
        status=row.status,
        title=row.title,
        message=row.message,
        source=row.source,
        details=row.details,
        created_at=row.created_at,
        acknowledged_at=row.acknowledged_at,
        acknowledged_by=row.acknowledged_by,
        resolved_at=row.resolved_at,
        resolved_by=row.resolved_by,
    )


async def _get_alert_or_404(db: AsyncSession, alert_id: uuid.UUID) -> Alert:
    row = (
        await db.execute(select(Alert).where(Alert.id == alert_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Алерт не найден")
    return row


def _audit(
    db: AsyncSession,
    admin: User,
    action: str,
    alert_id: uuid.UUID,
    details: dict[str, Any] | None = None,
) -> None:
    db.add(
        AdminAuditLog(
            actor_id=admin.id,
            actor_email=admin.email,
            action=action,
            target_type="alert",
            target_id=str(alert_id),
            details=details,
        )
    )


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get("", response_model=AlertListResponse)
async def list_alerts(
    status: AlertStatus | None = Query(None),
    severity: AlertSeverity | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AlertListResponse:
    base = select(Alert)
    if status is not None:
        base = base.where(Alert.status == status)
    if severity is not None:
        base = base.where(Alert.severity == severity)

    total = int(
        (
            await db.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar_one()
        or 0
    )

    rows = (
        await db.execute(
            base.order_by(Alert.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).scalars().all()

    # unread_count is *always* the global count of new alerts, regardless of
    # which filter the admin is browsing — that's the sidebar badge source.
    unread_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Alert)
                .where(Alert.status == "new")
            )
        ).scalar_one()
        or 0
    )

    return AlertListResponse(
        items=[_to_item(r) for r in rows],
        total=total,
        page=page,
        per_page=per_page,
        unread_count=unread_count,
    )


@router.post("/{alert_id}/acknowledge", response_model=AlertItem)
async def acknowledge_alert(
    alert_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AlertItem:
    alert = await _get_alert_or_404(db, alert_id)
    if alert.status == "resolved":
        # Already resolved — return current state without changes.
        return _to_item(alert)
    now = datetime.now(timezone.utc)
    alert.status = "acknowledged"
    alert.acknowledged_at = now
    alert.acknowledged_by = admin.id
    _audit(db, admin, "admin.alert.acknowledge", alert_id)
    await db.commit()
    return _to_item(alert)


@router.post("/{alert_id}/resolve", response_model=AlertItem)
async def resolve_alert(
    alert_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AlertItem:
    alert = await _get_alert_or_404(db, alert_id)
    now = datetime.now(timezone.utc)
    alert.status = "resolved"
    alert.resolved_at = now
    alert.resolved_by = admin.id
    _audit(db, admin, "admin.alert.resolve", alert_id)
    await db.commit()
    return _to_item(alert)
