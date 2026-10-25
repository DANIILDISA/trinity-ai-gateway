"""Admin request-tracking endpoint (FEATURE 1).

GET /api/v1/admin/requests — paginated usage_logs with user + model joined,
optional search by request_id (full UUID or prefix).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import String as SA_String

from app.core.constants import MICRO_PER_UNIT, micro_to_api_string
from app.dependencies import get_db, require_admin
from app.models.model_pricing import ModelPricing
from app.models.usage_log import UsageLog
from app.models.user import User

router = APIRouter()


class RequestRow(BaseModel):
    request_id: str
    created_at: datetime
    completed_at: datetime | None
    user_id: str
    user_email: str | None
    model_id: str
    model_name: str | None
    status: str
    response_received: bool
    input_tokens: int
    output_tokens: int
    prompt_length_chars: int | None
    openrouter_cost_micro_usd: int
    openrouter_cost_usd: float
    our_cost_micro_rub: int
    our_cost_display: str
    user_cost_micro_rub: int
    user_cost_display: str


class RequestListResponse(BaseModel):
    items: list[RequestRow]
    total: int
    page: int
    per_page: int


@router.get("", response_model=RequestListResponse)
async def list_requests(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    request_id: str | None = Query(None, description="Full UUID or prefix"),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> RequestListResponse:
    base = (
        select(UsageLog, User.email, ModelPricing.model_name)
        .outerjoin(User, User.id == UsageLog.user_id)
        .outerjoin(
            ModelPricing,
            (ModelPricing.model_id == UsageLog.model_id)
            & (ModelPricing.is_current.is_(True)),
        )
    )

    if request_id:
        token = request_id.strip()
        try:
            base = base.where(UsageLog.request_id == uuid.UUID(token))
        except ValueError:
            # Prefix search on the textual UUID (e.g. the short id shown in chat).
            base = base.where(
                cast(UsageLog.request_id, SA_String).ilike(f"{token}%")
            )

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0

    rows = (
        await db.execute(
            base.order_by(UsageLog.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).all()

    items: list[RequestRow] = []
    for log, email, model_name in rows:
        our_cost_micro_rub = (
            log.openrouter_cost_micro_usd * log.exchange_rate_micro_rub
        ) // MICRO_PER_UNIT
        items.append(
            RequestRow(
                request_id=str(log.request_id),
                created_at=log.created_at,
                completed_at=log.completed_at,
                user_id=str(log.user_id),
                user_email=email,
                model_id=log.model_id,
                model_name=model_name,
                status=log.status,
                response_received=log.status in ("completed", "partial"),
                input_tokens=log.input_tokens,
                output_tokens=log.output_tokens,
                prompt_length_chars=log.prompt_length_chars,
                openrouter_cost_micro_usd=log.openrouter_cost_micro_usd,
                openrouter_cost_usd=log.openrouter_cost_micro_usd / MICRO_PER_UNIT,
                our_cost_micro_rub=our_cost_micro_rub,
                our_cost_display=micro_to_api_string(our_cost_micro_rub),
                user_cost_micro_rub=log.actual_cost_micro_rub,
                user_cost_display=micro_to_api_string(log.actual_cost_micro_rub),
            )
        )

    return RequestListResponse(
        items=items, total=int(total), page=page, per_page=per_page
    )
