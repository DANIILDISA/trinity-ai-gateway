"""Admin user management endpoints (Tasks 8.1 + 8.3).

Endpoints (all require `require_admin`):
  GET    /api/v1/admin/users                — search/list with sort + pagination
  GET    /api/v1/admin/users/{id}           — detailed profile with stats
  POST   /api/v1/admin/users/{id}/block     — block + audit log
  POST   /api/v1/admin/users/{id}/unblock   — unblock + audit log
  POST   /api/v1/admin/users/{id}/adjust-balance — credit/debit balance + audit
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import MICRO_PER_UNIT, micro_to_api_string
from app.dependencies import get_db, require_admin
from app.models.audit_log import AdminAuditLog
from app.models.refresh_token import RefreshToken
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services.billing_service import admin_adjust_balance

router = APIRouter()


# ── Schemas ─────────────────────────────────────────────────────────────────


class AdminUserSummary(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: str
    status: str
    balance: str
    balance_micro_rub: int
    created_at: datetime
    last_active_at: datetime | None


class AdminUserListResponse(BaseModel):
    items: list[AdminUserSummary]
    total: int
    page: int
    per_page: int


class AdminUserStats(BaseModel):
    total_topped_up_micro_rub: int
    total_topped_up_display: str
    total_spent_micro_rub: int
    total_spent_display: str
    total_requests: int
    total_tokens: int
    avg_request_cost_micro_rub: int
    avg_request_cost_display: str
    requests_today: int
    most_used_model: str | None


class AdminUserDetail(AdminUserSummary):
    block_reason: str | None
    has_password: bool
    has_yandex: bool
    sessions_count: int
    stats: AdminUserStats


class BlockUserRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


# tr-lzmr: caps a single adjustment well below Postgres BIGINT range
# (±9.2e18) so an out-of-range value is rejected here with a clean 422
# instead of reaching the transactions INSERT as a raw asyncpg.DataError.
# 100M rubles is far beyond any legitimate single adjustment.
_MAX_ADJUST_AMOUNT_RUB = 100_000_000
_MAX_ADJUST_AMOUNT_MICRO_RUB = _MAX_ADJUST_AMOUNT_RUB * MICRO_PER_UNIT


class AdjustBalanceRequest(BaseModel):
    # Two accepted shapes: legacy `amount` (whole rubles) — kept so that
    # earlier admin tooling and tests keep working — and the new
    # `amount_micro_rub` (raw micro-rubles) per Task 8.3.
    amount: int | None = Field(
        default=None,
        description="Whole rubles (legacy)",
        ge=-_MAX_ADJUST_AMOUNT_RUB,
        le=_MAX_ADJUST_AMOUNT_RUB,
    )
    amount_micro_rub: int | None = Field(
        default=None,
        description="Micro-rubles",
        ge=-_MAX_ADJUST_AMOUNT_MICRO_RUB,
        le=_MAX_ADJUST_AMOUNT_MICRO_RUB,
    )
    reason: str = Field(min_length=5, max_length=1000)

    @model_validator(mode="after")
    def exactly_one_amount(self) -> "AdjustBalanceRequest":
        provided = (self.amount is not None) + (self.amount_micro_rub is not None)
        if provided == 0:
            raise ValueError("Provide amount or amount_micro_rub.")
        if provided > 1:
            raise ValueError("Provide only one of amount / amount_micro_rub.")
        return self

    @property
    def effective_micro_rub(self) -> int:
        if self.amount_micro_rub is not None:
            return self.amount_micro_rub
        assert self.amount is not None
        return self.amount * MICRO_PER_UNIT


class AdjustBalanceResponse(BaseModel):
    balance_micro_rub: int
    balance: str
    balance_display: str
    transaction_id: str


# ── Helpers ─────────────────────────────────────────────────────────────────


def _to_summary(user: User) -> AdminUserSummary:
    return AdminUserSummary(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
        balance=micro_to_api_string(user.balance_micro_rub),
        balance_micro_rub=user.balance_micro_rub,
        created_at=user.created_at,
        last_active_at=user.last_active_at,
    )


async def _get_user_or_404(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return user


SortBy = Literal["created_at", "last_active_at", "balance_micro_rub", "email"]
SortOrder = Literal["asc", "desc"]


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get("", response_model=AdminUserListResponse)
async def list_users(
    search: str | None = Query(None, max_length=255),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    sort_by: SortBy = Query("created_at"),
    sort_order: SortOrder = Query("desc"),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminUserListResponse:
    base = select(User)
    if search:
        like = f"%{search}%"
        base = base.where(
            (User.email.ilike(like)) | (User.display_name.ilike(like))
        )

    total = int(
        (
            await db.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar_one()
        or 0
    )

    sort_col_map = {
        "created_at": User.created_at,
        "last_active_at": User.last_active_at,
        "balance_micro_rub": User.balance_micro_rub,
        "email": User.email,
    }
    col = sort_col_map[sort_by]
    ordered = base.order_by(desc(col) if sort_order == "desc" else col)
    rows = (
        await db.execute(
            ordered.offset((page - 1) * per_page).limit(per_page)
        )
    ).scalars().all()

    return AdminUserListResponse(
        items=[_to_summary(u) for u in rows],
        total=total,
        page=page,
        per_page=per_page,
    )


async def _gather_stats(db: AsyncSession, user_id: uuid.UUID) -> AdminUserStats:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Deposited = ALL credit-like inflows (credit top-ups + refunds + positive
    # admin adjustments), i.e. every transaction with a positive amount. Bucketing
    # by `type == 'credit'` silently dropped admin adjustments and refunds, so
    # "Всего пополнено" undercounted and deposited − spent ≠ balance (tr-ej0w).
    total_topped_up = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(Transaction.amount_micro_rub), 0))
                .where(Transaction.user_id == user_id)
                .where(Transaction.amount_micro_rub > 0)
            )
        ).scalar_one()
        or 0
    )

    # Spent = ALL outflows (debits + negative admin adjustments), as a positive
    # quantity. Bucketing by sign (not by `type == 'debit'`) keeps negative admin
    # adjustments in spend so the identity holds. Overdraft-absorbed cost is never
    # a transaction (it lives only in usage_logs.overdraft_absorbed_micro_rub), so
    # it correctly stays out of both deposited and spent.
    total_spent = int(
        (
            await db.execute(
                select(
                    func.coalesce(
                        func.sum(func.abs(Transaction.amount_micro_rub)), 0
                    )
                )
                .where(Transaction.user_id == user_id)
                .where(Transaction.amount_micro_rub < 0)
            )
        ).scalar_one()
        or 0
    )

    total_requests = int(
        (
            await db.execute(
                select(func.count()).select_from(UsageLog).where(
                    UsageLog.user_id == user_id
                )
            )
        ).scalar_one()
        or 0
    )

    total_tokens = int(
        (
            await db.execute(
                select(
                    func.coalesce(
                        func.sum(UsageLog.input_tokens + UsageLog.output_tokens),
                        0,
                    )
                ).where(UsageLog.user_id == user_id)
            )
        ).scalar_one()
        or 0
    )

    # AVG over the last 100 completed requests (BILLING_LOGIC §13 / SPEC §7.6).
    last100 = (
        select(UsageLog.actual_cost_micro_rub)
        .where(UsageLog.user_id == user_id)
        .where(UsageLog.status == "completed")
        .order_by(UsageLog.created_at.desc())
        .limit(100)
        .subquery()
    )
    avg_cost = (
        await db.execute(select(func.avg(last100.c.actual_cost_micro_rub)))
    ).scalar_one()
    avg_cost_micro = int(avg_cost) if avg_cost is not None else 0

    requests_today = int(
        (
            await db.execute(
                select(func.count())
                .select_from(UsageLog)
                .where(UsageLog.user_id == user_id)
                .where(UsageLog.created_at >= today_start)
            )
        ).scalar_one()
        or 0
    )

    # Most-used model — pick the model_id with the largest count.
    most_used_row = (
        await db.execute(
            select(UsageLog.model_id, func.count().label("c"))
            .where(UsageLog.user_id == user_id)
            .group_by(UsageLog.model_id)
            .order_by(func.count().desc())
            .limit(1)
        )
    ).first()
    most_used_model = most_used_row[0] if most_used_row else None

    return AdminUserStats(
        total_topped_up_micro_rub=total_topped_up,
        total_topped_up_display=micro_to_api_string(total_topped_up),
        total_spent_micro_rub=total_spent,
        total_spent_display=micro_to_api_string(total_spent),
        total_requests=total_requests,
        total_tokens=total_tokens,
        avg_request_cost_micro_rub=avg_cost_micro,
        avg_request_cost_display=micro_to_api_string(avg_cost_micro),
        requests_today=requests_today,
        most_used_model=most_used_model,
    )


@router.get("/{user_id}", response_model=AdminUserDetail)
async def get_user_detail(
    user_id: uuid.UUID,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminUserDetail:
    user = await _get_user_or_404(db, user_id)
    now = datetime.now(timezone.utc)

    sessions_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(RefreshToken)
                .where(RefreshToken.user_id == user_id)
                .where(RefreshToken.expires_at > now)
            )
        ).scalar_one()
        or 0
    )

    stats = await _gather_stats(db, user_id)

    summary = _to_summary(user)
    return AdminUserDetail(
        **summary.model_dump(),
        block_reason=user.block_reason,
        has_password=user.password_hash is not None,
        has_yandex=user.yandex_id is not None,
        sessions_count=sessions_count,
        stats=stats,
    )


def _audit(
    db: AsyncSession,
    admin: User,
    action: str,
    target_user_id: uuid.UUID,
    details: dict[str, Any] | None = None,
) -> None:
    db.add(
        AdminAuditLog(
            actor_id=admin.id,
            actor_email=admin.email,
            action=action,
            target_type="user",
            target_id=str(target_user_id),
            details=details,
        )
    )


@router.post("/{user_id}/block", response_model=AdminUserSummary)
async def block_user(
    user_id: uuid.UUID,
    body: BlockUserRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminUserSummary:
    user = await _get_user_or_404(db, user_id)
    if user.id == admin.id:
        raise HTTPException(
            status_code=400, detail="Нельзя заблокировать самого себя"
        )
    user.status = "blocked"
    user.block_reason = body.reason
    user.updated_at = datetime.now(timezone.utc)
    _audit(
        db,
        admin,
        "admin.user.block",
        user_id,
        details={"reason": body.reason},
    )
    await db.commit()
    return _to_summary(user)


@router.post("/{user_id}/unblock", response_model=AdminUserSummary)
async def unblock_user(
    user_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminUserSummary:
    user = await _get_user_or_404(db, user_id)
    user.status = "active"
    user.block_reason = None
    user.updated_at = datetime.now(timezone.utc)
    _audit(db, admin, "admin.user.unblock", user_id)
    await db.commit()
    return _to_summary(user)


@router.post("/{user_id}/adjust-balance", response_model=AdjustBalanceResponse)
async def adjust_balance(
    user_id: uuid.UUID,
    body: AdjustBalanceRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdjustBalanceResponse:
    effective = body.effective_micro_rub
    if effective == 0:
        raise HTTPException(status_code=422, detail="Сумма не может быть нулевой")

    new_balance, txn_id = await admin_adjust_balance(
        db, admin.id, user_id, effective, body.reason
    )
    await db.commit()

    display = micro_to_api_string(new_balance)
    return AdjustBalanceResponse(
        balance_micro_rub=new_balance,
        balance=display,
        balance_display=display,
        transaction_id=str(txn_id),
    )
