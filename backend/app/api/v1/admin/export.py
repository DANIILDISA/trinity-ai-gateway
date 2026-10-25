"""Admin data export endpoints (Task 8.6).

POST /api/v1/admin/export/transactions  — CSV of every transaction in [from, to]
POST /api/v1/admin/export/summary       — JSON aggregate (revenue/cost/margin/users/requests)
POST /api/v1/admin/export/per-user      — CSV of per-user spending
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import MICRO_PER_UNIT, micro_to_api_string
from app.dependencies import get_db, require_admin
from app.models.audit_log import AdminAuditLog
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services.tariff_service import _get_pricing_settings

router = APIRouter()


# ── Schemas ─────────────────────────────────────────────────────────────────


class DateRange(BaseModel):
    date_from: date
    date_to: date


class PeriodOut(BaseModel):
    date_from: str
    date_to: str


class RevenueBlock(BaseModel):
    total_micro_rub: int
    total_display: str
    credit_count: int
    debit_count: int


class CostsBlock(BaseModel):
    total_micro_usd: int
    total_usd_display: str


class MarginBlock(BaseModel):
    percent: float
    effective_revenue_micro_rub: int


class UsersBlock(BaseModel):
    unique_payers: int
    new_registrations: int


class RequestsBlock(BaseModel):
    total: int
    completed: int
    failed: int
    partial: int


class ExportSummaryResponse(BaseModel):
    period: PeriodOut
    revenue: RevenueBlock
    costs: CostsBlock
    margin: MarginBlock
    users: UsersBlock
    requests: RequestsBlock


# ── Helpers ─────────────────────────────────────────────────────────────────


def _bounds(body: DateRange) -> tuple[datetime, datetime]:
    start = datetime.combine(body.date_from, datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.combine(
        body.date_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    )
    return start, end


def _audit(db: AsyncSession, admin: User, action: str, details: dict) -> None:
    db.add(
        AdminAuditLog(
            actor_id=admin.id,
            actor_email=admin.email,
            action=action,
            target_type="export",
            target_id=None,
            details=details,
        )
    )


def _csv_response(csv_bytes: bytes, filename: str) -> Response:
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.post("/transactions")
async def export_transactions(
    body: DateRange,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> Response:
    start, end = _bounds(body)
    rows = (
        await db.execute(
            text(
                """
                SELECT
                    t.created_at,
                    t.type,
                    t.amount_micro_rub,
                    t.balance_after_micro_rub,
                    t.description,
                    u.email as user_email,
                    t.payment_id,
                    t.admin_reason
                FROM transactions t
                JOIN users u ON u.id = t.user_id
                WHERE t.created_at >= :start AND t.created_at < :end
                ORDER BY t.created_at
                """
            ),
            {"start": start, "end": end},
        )
    ).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "Дата",
            "Тип",
            "Сумма (₽)",
            "Баланс после (₽)",
            "Описание",
            "Email",
            "ID платежа",
            "Причина",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r.created_at.isoformat(),
                r.type,
                micro_to_api_string(r.amount_micro_rub),
                micro_to_api_string(r.balance_after_micro_rub),
                r.description,
                r.user_email,
                r.payment_id or "",
                r.admin_reason or "",
            ]
        )

    payload = buf.getvalue().encode("utf-8-sig")
    filename = f"transactions_{body.date_from}_{body.date_to}.csv"
    _audit(
        db,
        admin,
        "admin.export.transactions",
        {
            "date_from": body.date_from.isoformat(),
            "date_to": body.date_to.isoformat(),
            "row_count": len(rows),
        },
    )
    await db.commit()
    return _csv_response(payload, filename)


@router.post("/summary", response_model=ExportSummaryResponse)
async def export_summary(
    body: DateRange,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ExportSummaryResponse:
    start, end = _bounds(body)
    exchange_rate, _ = await _get_pricing_settings(db)

    revenue_micro = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(func.abs(Transaction.amount_micro_rub)), 0))
                .where(Transaction.type == "debit")
                .where(Transaction.created_at >= start)
                .where(Transaction.created_at < end)
            )
        ).scalar_one()
        or 0
    )
    credit_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Transaction)
                .where(Transaction.type == "credit")
                .where(Transaction.created_at >= start)
                .where(Transaction.created_at < end)
            )
        ).scalar_one()
        or 0
    )
    debit_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Transaction)
                .where(Transaction.type == "debit")
                .where(Transaction.created_at >= start)
                .where(Transaction.created_at < end)
            )
        ).scalar_one()
        or 0
    )

    overdraft = int(
        (
            await db.execute(
                select(
                    func.coalesce(func.sum(UsageLog.overdraft_absorbed_micro_rub), 0)
                )
                .where(UsageLog.status.in_(("completed", "partial")))
                .where(UsageLog.created_at >= start)
                .where(UsageLog.created_at < end)
            )
        ).scalar_one()
        or 0
    )
    cost_micro_usd = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(UsageLog.openrouter_cost_micro_usd), 0))
                .where(UsageLog.status.in_(("completed", "partial")))
                .where(UsageLog.created_at >= start)
                .where(UsageLog.created_at < end)
            )
        ).scalar_one()
        or 0
    )

    effective_revenue = revenue_micro - overdraft
    cost_micro_rub = (cost_micro_usd * exchange_rate) // MICRO_PER_UNIT
    margin_percent = (
        (effective_revenue - cost_micro_rub) / effective_revenue * 100
        if effective_revenue > 0
        else 0.0
    )

    unique_payers = int(
        (
            await db.execute(
                select(func.count(func.distinct(Transaction.user_id)))
                .where(Transaction.type == "credit")
                .where(Transaction.created_at >= start)
                .where(Transaction.created_at < end)
            )
        ).scalar_one()
        or 0
    )
    new_registrations = int(
        (
            await db.execute(
                select(func.count())
                .select_from(User)
                .where(User.created_at >= start)
                .where(User.created_at < end)
            )
        ).scalar_one()
        or 0
    )

    requests_total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(UsageLog)
                .where(UsageLog.created_at >= start)
                .where(UsageLog.created_at < end)
            )
        ).scalar_one()
        or 0
    )

    async def _count_status(status: str) -> int:
        return int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(UsageLog)
                    .where(UsageLog.status == status)
                    .where(UsageLog.created_at >= start)
                    .where(UsageLog.created_at < end)
                )
            ).scalar_one()
            or 0
        )

    completed = await _count_status("completed")
    failed = await _count_status("failed")
    partial = await _count_status("partial")

    _audit(
        db,
        admin,
        "admin.export.summary",
        {
            "date_from": body.date_from.isoformat(),
            "date_to": body.date_to.isoformat(),
        },
    )
    await db.commit()

    return ExportSummaryResponse(
        period=PeriodOut(
            date_from=body.date_from.isoformat(),
            date_to=body.date_to.isoformat(),
        ),
        revenue=RevenueBlock(
            total_micro_rub=revenue_micro,
            total_display=micro_to_api_string(revenue_micro),
            credit_count=credit_count,
            debit_count=debit_count,
        ),
        costs=CostsBlock(
            total_micro_usd=cost_micro_usd,
            total_usd_display=f"{cost_micro_usd / MICRO_PER_UNIT:.4f}",
        ),
        margin=MarginBlock(
            percent=margin_percent,
            effective_revenue_micro_rub=effective_revenue,
        ),
        users=UsersBlock(
            unique_payers=unique_payers,
            new_registrations=new_registrations,
        ),
        requests=RequestsBlock(
            total=requests_total,
            completed=completed,
            failed=failed,
            partial=partial,
        ),
    )


@router.post("/per-user")
async def export_per_user(
    body: DateRange,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> Response:
    start, end = _bounds(body)
    rows = (
        await db.execute(
            text(
                """
                SELECT
                    u.email,
                    u.display_name,
                    u.created_at as registered_at,
                    -- Deposited / spent bucketed by SIGN, not type, so admin
                    -- adjustments and refunds are not silently dropped (tr-ej0w).
                    COALESCE(SUM(CASE WHEN t.amount_micro_rub > 0 THEN t.amount_micro_rub ELSE 0 END), 0) AS topped_up_micro_rub,
                    COALESCE(SUM(CASE WHEN t.amount_micro_rub < 0 THEN ABS(t.amount_micro_rub) ELSE 0 END), 0) AS spent_micro_rub,
                    COALESCE(ul.request_count, 0) AS request_count,
                    COALESCE(ul.token_count, 0) AS token_count
                FROM users u
                LEFT JOIN transactions t
                    ON t.user_id = u.id
                    AND t.created_at >= :start
                    AND t.created_at < :end
                LEFT JOIN LATERAL (
                    SELECT
                        COUNT(*) AS request_count,
                        COALESCE(SUM(input_tokens + output_tokens), 0) AS token_count
                    FROM usage_logs
                    WHERE user_id = u.id
                      AND created_at >= :start
                      AND created_at < :end
                ) ul ON true
                GROUP BY u.id, u.email, u.display_name, u.created_at,
                         ul.request_count, ul.token_count
                HAVING COUNT(t.id) > 0
                    OR COALESCE(ul.request_count, 0) > 0
                ORDER BY spent_micro_rub DESC
                """
            ),
            {"start": start, "end": end},
        )
    ).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "Email",
            "Имя",
            "Пополнено (₽)",
            "Потрачено (₽)",
            "Запросов",
            "Токенов",
            "Дата регистрации",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r.email,
                r.display_name,
                micro_to_api_string(int(r.topped_up_micro_rub)),
                micro_to_api_string(int(r.spent_micro_rub)),
                int(r.request_count),
                int(r.token_count),
                r.registered_at.isoformat(),
            ]
        )

    payload = buf.getvalue().encode("utf-8-sig")
    filename = f"per_user_{body.date_from}_{body.date_to}.csv"
    _audit(
        db,
        admin,
        "admin.export.per_user",
        {
            "date_from": body.date_from.isoformat(),
            "date_to": body.date_to.isoformat(),
            "row_count": len(rows),
        },
    )
    await db.commit()
    return _csv_response(payload, filename)
