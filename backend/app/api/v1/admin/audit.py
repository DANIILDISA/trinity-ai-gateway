"""Admin audit log endpoint (Task 8.5).

GET /api/v1/admin/audit — paginated audit log with action/actor/date filters.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_admin
from app.models.audit_log import AdminAuditLog
from app.models.user import User

router = APIRouter()


# ── Schemas ─────────────────────────────────────────────────────────────────


class AuditLogItem(BaseModel):
    id: uuid.UUID
    action: str
    actor_id: uuid.UUID | None
    actor_email: str | None
    target_type: str | None
    target_id: str | None
    details: dict[str, Any] | None
    created_at: datetime
    ip_address: str | None


class AuditLogResponse(BaseModel):
    items: list[AuditLogItem]
    total: int
    page: int
    per_page: int


# ── Endpoint ────────────────────────────────────────────────────────────────


@router.get("", response_model=AuditLogResponse)
async def list_audit_log(
    action: str | None = Query(None, max_length=128),
    actor_id: uuid.UUID | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AuditLogResponse:
    base = select(AdminAuditLog)
    if action:
        base = base.where(AdminAuditLog.action.ilike(f"%{action}%"))
    if actor_id is not None:
        base = base.where(AdminAuditLog.actor_id == actor_id)
    if date_from is not None:
        base = base.where(
            AdminAuditLog.created_at
            >= datetime.combine(date_from, datetime.min.time(), tzinfo=timezone.utc)
        )
    if date_to is not None:
        # Inclusive of the end-of-day; advance one day and use strict <.
        next_day = datetime.combine(
            date_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
        )
        base = base.where(AdminAuditLog.created_at < next_day)

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
            base.order_by(AdminAuditLog.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).scalars().all()

    return AuditLogResponse(
        items=[
            AuditLogItem(
                id=r.id,
                action=r.action,
                actor_id=r.actor_id,
                actor_email=r.actor_email,
                target_type=r.target_type,
                target_id=r.target_id,
                details=r.details,
                created_at=r.created_at,
                ip_address=r.ip_address,
            )
            for r in rows
        ],
        total=total,
        page=page,
        per_page=per_page,
    )
