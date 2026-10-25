"""Financial dashboard endpoints (Task 8.2).

GET /api/v1/admin/dashboard               — aggregated metrics (today/week/month)
GET /api/v1/admin/dashboard/revenue-by-model — per-model breakdown for one period

Margin math follows BILLING_LOGIC.md §13.1 — effective revenue subtracts
overdraft_absorbed_micro_rub so the operator isn't credited for amounts they
actually absorbed.
"""
from datetime import datetime, timedelta, timezone
from typing import Literal

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import MICRO_PER_UNIT, micro_to_api_string
from app.dependencies import get_db, get_redis, require_admin
from app.models.model_pricing import ModelPricing
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services.tariff_service import _get_pricing_settings

router = APIRouter()

Period = Literal["today", "week", "month"]


# ── Schemas ─────────────────────────────────────────────────────────────────


class RevenueBlock(BaseModel):
    today_micro_rub: int
    today_display: str
    week_micro_rub: int
    week_display: str
    month_micro_rub: int
    month_display: str


class CostsBlock(BaseModel):
    today_usd: float
    week_usd: float
    month_usd: float


class MarginBlock(BaseModel):
    today_percent: float
    week_percent: float
    month_percent: float


class ReconciliationBlock(BaseModel):
    status: str
    discrepancy_percent: float
    last_check: datetime


class DashboardResponse(BaseModel):
    revenue: RevenueBlock
    # Overdraft absorbed by the operator (cost it couldn't recover) and the
    # effective revenue the margin is based on (revenue − overdraft). Surfaced so
    # a depressed margin reads as "absorbed losses" vs a pricing fault (tr-yalc).
    overdraft: RevenueBlock
    effective_revenue: RevenueBlock
    costs: CostsBlock
    margin: MarginBlock
    reconciliation: ReconciliationBlock
    openrouter_balance_usd: float | None
    openrouter_balance_warning: bool
    openrouter_balance_source: Literal["live", "manual", "depleted", "unavailable"]
    active_users_today: int
    total_requests_today: int


class ModelRevenueRow(BaseModel):
    model_id: str
    model_name: str
    request_count: int
    revenue_micro_rub: int
    revenue_display: str
    cost_micro_usd: int
    cost_usd: float
    margin_percent: float
    total_input_tokens: int
    total_output_tokens: int


class RevenueByModelResponse(BaseModel):
    models: list[ModelRevenueRow]


# ── Helpers ─────────────────────────────────────────────────────────────────


def _utc_today_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _get_date_range(period: Period, now: datetime) -> tuple[datetime, datetime]:
    today_start = _utc_today_start(now)
    if period == "today":
        return today_start, now
    if period == "week":
        return now - timedelta(days=7), now
    if period == "month":
        return now - timedelta(days=30), now
    raise HTTPException(status_code=422, detail="Invalid period")


async def _sum_revenue_micro_rub(
    db: AsyncSession,
    start: datetime,
    end: datetime,
    *,
    only_known_cost: bool = False,
) -> int:
    """Sum of debits in (start, end). amount_micro_rub for debits is negative;
    use ABS to express gross revenue as a positive quantity.

    only_known_cost=True excludes debits whose usage_log is still awaiting cost
    backfill (cost_pending) — used as the MARGIN denominator so an unknown-cost
    request can't inflate margin to ~100% (tr-34mj). The default (all debits)
    feeds the headline revenue figure.
    """
    stmt = (
        select(func.coalesce(func.sum(func.abs(Transaction.amount_micro_rub)), 0))
        .where(Transaction.type == "debit")
        .where(Transaction.created_at >= start)
        .where(Transaction.created_at < end)
    )
    if only_known_cost:
        pending_requests = select(UsageLog.request_id).where(
            UsageLog.cost_pending.is_(True)
        )
        stmt = stmt.where(Transaction.request_id.not_in(pending_requests))
    result = await db.execute(stmt)
    return int(result.scalar_one() or 0)


async def _sum_overdraft_absorbed(
    db: AsyncSession, start: datetime, end: datetime
) -> int:
    result = await db.execute(
        select(
            func.coalesce(func.sum(UsageLog.overdraft_absorbed_micro_rub), 0)
        )
        .where(UsageLog.status.in_(("completed", "partial")))
        .where(UsageLog.created_at >= start)
        .where(UsageLog.created_at < end)
    )
    return int(result.scalar_one() or 0)


async def _sum_cost_micro_usd(
    db: AsyncSession, start: datetime, end: datetime
) -> int:
    result = await db.execute(
        select(func.coalesce(func.sum(UsageLog.openrouter_cost_micro_usd), 0))
        .where(UsageLog.status.in_(("completed", "partial")))
        .where(UsageLog.created_at >= start)
        .where(UsageLog.created_at < end)
    )
    return int(result.scalar_one() or 0)


def _margin_percent(
    effective_revenue_micro_rub: int, cost_micro_rub: int
) -> float:
    if effective_revenue_micro_rub <= 0:
        return 0.0
    return (
        (effective_revenue_micro_rub - cost_micro_rub)
        / effective_revenue_micro_rub
        * 100
    )


def _micro_usd_to_micro_rub(micro_usd: int, exchange_rate_micro_rub: int) -> int:
    """BILLING_LOGIC.md uses the same micro-USD × exchange / 10^6 formula."""
    return (micro_usd * exchange_rate_micro_rub) // MICRO_PER_UNIT


async def _period_block(
    db: AsyncSession,
    start: datetime,
    end: datetime,
    exchange_rate: int,
) -> tuple[int, int, float, float, int]:
    """Returns (revenue_micro_rub, cost_micro_usd, cost_usd_float, margin_percent,
    overdraft_absorbed_micro_rub)."""
    revenue = await _sum_revenue_micro_rub(db, start, end)
    # Margin denominator excludes revenue from requests whose cost is still
    # pending backfill, so the displayed margin reflects only requests where both
    # sides are known (tr-34mj). Headline `revenue` keeps all charges.
    margin_revenue = await _sum_revenue_micro_rub(
        db, start, end, only_known_cost=True
    )
    overdraft = await _sum_overdraft_absorbed(db, start, end)
    cost_micro_usd = await _sum_cost_micro_usd(db, start, end)

    effective = margin_revenue - overdraft
    cost_micro_rub = _micro_usd_to_micro_rub(cost_micro_usd, exchange_rate)
    margin = _margin_percent(effective, cost_micro_rub)
    return revenue, cost_micro_usd, cost_micro_usd / MICRO_PER_UNIT, margin, overdraft


async def _reconciliation_check(db: AsyncSession) -> ReconciliationBlock:
    """Per-user check: is balance_micro_rub == SUM(transactions.amount_micro_rub)?

    Returns 'ok' / 'discrepancy' with the percent of users whose ledgers disagree
    with their stored balance. Cheap-enough at MVP scale; if it becomes a
    hot-spot we can cache or move to a nightly job.
    """
    total_users = int(
        (await db.execute(select(func.count()).select_from(User))).scalar_one() or 0
    )
    if total_users == 0:
        return ReconciliationBlock(
            status="ok",
            discrepancy_percent=0.0,
            last_check=datetime.now(timezone.utc),
        )

    txn_sum = (
        select(
            Transaction.user_id.label("uid"),
            func.coalesce(func.sum(Transaction.amount_micro_rub), 0).label("s"),
        )
        .group_by(Transaction.user_id)
        .subquery()
    )

    bad_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(User)
                .outerjoin(txn_sum, User.id == txn_sum.c.uid)
                .where(
                    User.balance_micro_rub
                    != func.coalesce(txn_sum.c.s, literal(0))
                )
            )
        ).scalar_one()
        or 0
    )

    status = "ok" if bad_count == 0 else "discrepancy"
    pct = (bad_count / total_users * 100) if bad_count else 0.0
    return ReconciliationBlock(
        status=status,
        discrepancy_percent=pct,
        last_check=datetime.now(timezone.utc),
    )


async def _balance_threshold(db: AsyncSession) -> float | None:
    from app.models.system_setting import SystemSetting

    raw = (
        await db.execute(
            select(SystemSetting.value).where(
                SystemSetting.key == "openrouter_balance_threshold_usd"
            )
        )
    ).scalar_one_or_none()
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


async def _openrouter_balance(
    db: AsyncSession, redis: Redis
) -> tuple[float, bool, str]:
    """Resolve the OpenRouter balance via the SHARED resolver so the dashboard
    and /admin/monitoring always agree (tr-ttot). See
    monitoring_service.resolve_openrouter_balance for live/manual/depleted
    semantics (the manual override no longer masks a real 402 depletion)."""
    from app.services.monitoring_service import resolve_openrouter_balance

    return await resolve_openrouter_balance(db, redis)


async def _get_manual_balance(db: AsyncSession) -> float | None:
    """Read the admin-entered openrouter_manual_balance_usd setting, or None."""
    from app.models.system_setting import SystemSetting

    raw = (
        await db.execute(
            select(SystemSetting.value).where(
                SystemSetting.key == "openrouter_manual_balance_usd"
            )
        )
    ).scalar_one_or_none()
    if raw is None:
        return None
    try:
        return float(Decimal(str(raw)))
    except (InvalidOperation, TypeError, ValueError):
        return None


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get("", response_model=DashboardResponse)
async def get_dashboard(
    period: Period = Query("today"),  # noqa: ARG001 — accepted but always full payload
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> DashboardResponse:
    now = datetime.now(timezone.utc)
    today_start = _utc_today_start(now)
    week_start = now - timedelta(days=7)
    month_start = now - timedelta(days=30)

    exchange_rate, _markup = await _get_pricing_settings(db)

    today_rev, today_cost_micro_usd, today_cost_usd, today_margin, today_ovr = (
        await _period_block(db, today_start, now, exchange_rate)
    )
    week_rev, _week_cmu, week_cost_usd, week_margin, week_ovr = await _period_block(
        db, week_start, now, exchange_rate
    )
    month_rev, _month_cmu, month_cost_usd, month_margin, month_ovr = (
        await _period_block(db, month_start, now, exchange_rate)
    )

    active_users_today = int(
        (
            await db.execute(
                select(func.count(func.distinct(UsageLog.user_id))).where(
                    UsageLog.created_at >= today_start
                )
            )
        ).scalar_one()
        or 0
    )
    total_requests_today = int(
        (
            await db.execute(
                select(func.count()).select_from(UsageLog).where(
                    UsageLog.created_at >= today_start
                )
            )
        ).scalar_one()
        or 0
    )

    reconciliation = await _reconciliation_check(db)
    or_balance, or_warning, or_source = await _openrouter_balance(db, redis)

    return DashboardResponse(
        revenue=RevenueBlock(
            today_micro_rub=today_rev,
            today_display=micro_to_api_string(today_rev),
            week_micro_rub=week_rev,
            week_display=micro_to_api_string(week_rev),
            month_micro_rub=month_rev,
            month_display=micro_to_api_string(month_rev),
        ),
        overdraft=RevenueBlock(
            today_micro_rub=today_ovr,
            today_display=micro_to_api_string(today_ovr),
            week_micro_rub=week_ovr,
            week_display=micro_to_api_string(week_ovr),
            month_micro_rub=month_ovr,
            month_display=micro_to_api_string(month_ovr),
        ),
        effective_revenue=RevenueBlock(
            today_micro_rub=today_rev - today_ovr,
            today_display=micro_to_api_string(today_rev - today_ovr),
            week_micro_rub=week_rev - week_ovr,
            week_display=micro_to_api_string(week_rev - week_ovr),
            month_micro_rub=month_rev - month_ovr,
            month_display=micro_to_api_string(month_rev - month_ovr),
        ),
        costs=CostsBlock(
            today_usd=today_cost_usd,
            week_usd=week_cost_usd,
            month_usd=month_cost_usd,
        ),
        margin=MarginBlock(
            today_percent=today_margin,
            week_percent=week_margin,
            month_percent=month_margin,
        ),
        reconciliation=reconciliation,
        openrouter_balance_usd=or_balance,
        openrouter_balance_warning=or_warning,
        openrouter_balance_source=or_source,
        active_users_today=active_users_today,
        total_requests_today=total_requests_today,
    )


@router.get("/revenue-by-model", response_model=RevenueByModelResponse)
async def get_revenue_by_model(
    period: Period = Query("today"),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> RevenueByModelResponse:
    now = datetime.now(timezone.utc)
    start, end = _get_date_range(period, now)

    exchange_rate, _markup = await _get_pricing_settings(db)

    stmt = (
        select(
            UsageLog.model_id,
            ModelPricing.model_name,
            func.count().label("request_count"),
            func.coalesce(func.sum(UsageLog.actual_cost_micro_rub), 0).label(
                "revenue_micro_rub"
            ),
            func.coalesce(func.sum(UsageLog.openrouter_cost_micro_usd), 0).label(
                "cost_micro_usd"
            ),
            func.coalesce(
                func.sum(UsageLog.overdraft_absorbed_micro_rub), 0
            ).label("overdraft"),
            # Revenue/overdraft restricted to rows whose OpenRouter cost is KNOWN
            # (cost_pending = false). Margin is computed over these only, so a
            # request still awaiting cost backfill can't inflate margin to ~100%
            # (tr-34mj). The displayed revenue above keeps ALL rows (real charges).
            func.coalesce(
                func.sum(UsageLog.actual_cost_micro_rub).filter(
                    ~UsageLog.cost_pending
                ),
                0,
            ).label("margin_revenue_micro_rub"),
            func.coalesce(
                func.sum(UsageLog.overdraft_absorbed_micro_rub).filter(
                    ~UsageLog.cost_pending
                ),
                0,
            ).label("margin_overdraft"),
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("in_tok"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("out_tok"),
        )
        .join(
            ModelPricing,
            (ModelPricing.model_id == UsageLog.model_id)
            & (ModelPricing.is_current.is_(True)),
        )
        .where(UsageLog.status.in_(("completed", "partial")))
        .where(UsageLog.created_at >= start)
        .where(UsageLog.created_at < end)
        .group_by(UsageLog.model_id, ModelPricing.model_name)
        .order_by(func.sum(UsageLog.actual_cost_micro_rub).desc())
    )

    rows = (await db.execute(stmt)).all()

    items: list[ModelRevenueRow] = []
    for r in rows:
        rev = int(r.revenue_micro_rub or 0)
        cost_micro_usd = int(r.cost_micro_usd or 0)
        cost_micro_rub = _micro_usd_to_micro_rub(cost_micro_usd, exchange_rate)
        # Margin over known-cost rows only (cost_pending excluded) — see query.
        margin_revenue = int(r.margin_revenue_micro_rub or 0)
        margin_overdraft = int(r.margin_overdraft or 0)
        margin = _margin_percent(margin_revenue - margin_overdraft, cost_micro_rub)

        items.append(
            ModelRevenueRow(
                model_id=r.model_id,
                model_name=r.model_name,
                request_count=int(r.request_count or 0),
                revenue_micro_rub=rev,
                revenue_display=micro_to_api_string(rev),
                cost_micro_usd=cost_micro_usd,
                cost_usd=cost_micro_usd / MICRO_PER_UNIT,
                margin_percent=margin,
                total_input_tokens=int(r.in_tok or 0),
                total_output_tokens=int(r.out_tok or 0),
            )
        )

    return RevenueByModelResponse(models=items)
