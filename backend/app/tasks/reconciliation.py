"""Daily financial reconciliation — BILLING_LOGIC.md §12.

Runs IN-PROCESS under the Redis leader lock (`_daily_reconciliation_loop` in
app/main.py, leader-only: once on startup + every 24h, tr-376l), so it fires even
where the host crontab is never installed. Do NOT also schedule it via host cron —
a host cron plus the in-process loop is the double-runner race (tr-6qf7). Verifies
four financial invariants:

1. Each user's `balance_micro_rub` equals SUM of their committed transactions.
2. No reservations have been sitting in `active` for >10 min (the cleanup
   cron should have released them).
3. Yesterday's revenue covers yesterday's OpenRouter cost. Overdraft-absorbed
   cost is added back into "effective revenue" so absorbed losses don't
   masquerade as a pricing problem.
4. No completed `usage_logs` row from yesterday lacks a matching debit
   transaction.

Discrepancies create `alerts` rows via `alert_service.create_alert`, which
dispatches Telegram + (for critical) email. The task always emits a
single Telegram summary at the end.

Run standalone ONLY for a one-off manual check (e.g. debugging) — never on a
schedule, since the in-process leader loop already owns the daily cadence:
    docker compose exec backend python -m app.tasks.reconciliation
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.constants import micro_usd_to_micro_rub
from app.core.database import dispose_engine, get_session_factory, init_engine
from app.services.alert_service import create_alert
from app.services.telegram_service import send_telegram_alert
from app.services.tariff_service import (
    _DEFAULT_EXCHANGE_RATE_MICRO_RUB,
    _get_setting,
)

logger = logging.getLogger(__name__)

STALE_RESERVATION_INTERVAL = "10 minutes"
MARGIN_CRITICAL_PERCENT = 0
MARGIN_WARNING_PERCENT = 10
PRICING_MARGIN_HEALTHY_PERCENT = 20


@dataclass
class ReconciliationResult:
    """Summary returned to the caller and used to build the Telegram message."""

    users_checked: int = 0
    transactions_checked: int = 0
    balance_check_passed: bool = True
    balance_mismatches: int = 0

    stale_reservations: int = 0

    margin_percent: float | None = None
    pricing_margin_percent: float | None = None
    total_revenue_micro_rub: int = 0
    total_cost_micro_rub: int = 0
    total_absorbed_micro_rub: int = 0

    orphaned_logs: int = 0

    alerts_created: list[tuple[str, str]] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return (
            self.balance_check_passed
            and self.stale_reservations == 0
            and self.orphaned_logs == 0
            and not any(sev != "info" for sev, _ in self.alerts_created)
        )


def _yesterday_bounds() -> tuple[datetime, datetime]:
    """UTC midnight-to-midnight bounds for yesterday."""
    today = date.today()
    yesterday = today - timedelta(days=1)
    day_start = datetime.combine(yesterday, time.min, tzinfo=timezone.utc)
    day_end = datetime.combine(today, time.min, tzinfo=timezone.utc)
    return day_start, day_end


# ── CHECK 1: balance == SUM(transactions) ───────────────────────────────────


async def _check_balance_invariant(
    db: AsyncSession, result: ReconciliationResult
) -> None:
    rows = (
        await db.execute(
            text(
                """
                SELECT
                    u.id::text AS uid,
                    u.email,
                    u.balance_micro_rub AS stored_balance,
                    COALESCE(SUM(t.amount_micro_rub), 0) AS computed_balance,
                    u.balance_micro_rub - COALESCE(SUM(t.amount_micro_rub), 0) AS difference
                FROM users u
                LEFT JOIN transactions t ON t.user_id = u.id
                GROUP BY u.id, u.email, u.balance_micro_rub
                HAVING u.balance_micro_rub != COALESCE(SUM(t.amount_micro_rub), 0)
                """
            )
        )
    ).all()

    # Independent counts (informational).
    result.users_checked = (
        await db.execute(text("SELECT COUNT(*) FROM users"))
    ).scalar() or 0
    result.transactions_checked = (
        await db.execute(text("SELECT COUNT(*) FROM transactions"))
    ).scalar() or 0

    result.balance_check_passed = len(rows) == 0
    result.balance_mismatches = len(rows)

    for row in rows:
        title = (
            f"Balance mismatch for user {row.email}: "
            f"stored={row.stored_balance}, computed={row.computed_balance}, "
            f"diff={row.difference}"
        )
        await create_alert(
            db,
            severity="critical",
            source="billing",
            title=title,
            details={
                "user_id": row.uid,
                "stored_balance_micro_rub": int(row.stored_balance),
                "computed_balance_micro_rub": int(row.computed_balance),
                "difference_micro_rub": int(row.difference),
            },
        )
        result.alerts_created.append(("critical", title))


# ── CHECK 2: stale active reservations ──────────────────────────────────────


async def _check_stale_reservations(
    db: AsyncSession, result: ReconciliationResult
) -> None:
    stale_count = (
        await db.execute(
            text(
                f"""
                SELECT COUNT(*)
                FROM reservations
                WHERE status = 'active'
                  AND created_at < now() - interval '{STALE_RESERVATION_INTERVAL}'
                """
            )
        )
    ).scalar() or 0

    result.stale_reservations = stale_count

    if stale_count > 0:
        title = (
            f"{stale_count} stale reservations found (>10 min old). "
            "Cleanup job may not be running."
        )
        await create_alert(
            db,
            severity="warning",
            source="billing",
            title=title,
            details={"stale_count": stale_count},
        )
        result.alerts_created.append(("warning", title))


# ── CHECK 3: revenue / cost with overdraft adjustment ───────────────────────


async def _check_revenue_vs_cost(
    db: AsyncSession, result: ReconciliationResult
) -> None:
    day_start, day_end = _yesterday_bounds()

    total_revenue = (
        await db.execute(
            text(
                """
                SELECT COALESCE(SUM(ABS(amount_micro_rub)), 0)
                FROM transactions
                WHERE type = 'debit'
                  AND created_at >= :day_start
                  AND created_at < :day_end
                """
            ),
            {"day_start": day_start, "day_end": day_end},
        )
    ).scalar() or 0

    cost_row = (
        await db.execute(
            text(
                """
                SELECT
                    COALESCE(SUM(openrouter_cost_micro_usd), 0) AS total_cost_micro_usd,
                    COALESCE(SUM(overdraft_absorbed_micro_rub), 0) AS total_absorbed_micro_rub
                FROM usage_logs
                WHERE status IN ('completed', 'partial')
                  AND created_at >= :day_start
                  AND created_at < :day_end
                """
            ),
            {"day_start": day_start, "day_end": day_end},
        )
    ).one()
    total_cost_micro_usd = int(cost_row.total_cost_micro_usd or 0)
    total_absorbed = int(cost_row.total_absorbed_micro_rub or 0)

    exchange_rate_raw = await _get_setting(db, "exchange_rate_micro_rub")
    exchange_rate = (
        int(exchange_rate_raw)
        if exchange_rate_raw is not None
        else _DEFAULT_EXCHANGE_RATE_MICRO_RUB
    )
    total_cost_micro_rub = micro_usd_to_micro_rub(
        total_cost_micro_usd, exchange_rate
    )

    result.total_revenue_micro_rub = int(total_revenue)
    result.total_cost_micro_rub = total_cost_micro_rub
    result.total_absorbed_micro_rub = total_absorbed

    if total_cost_micro_rub <= 0:
        # No cost yesterday → nothing to compare. Margin stays None.
        return

    effective_revenue = int(total_revenue) + total_absorbed
    margin_percent = (
        (int(total_revenue) - total_cost_micro_rub) / total_cost_micro_rub * 100
    )
    pricing_margin_percent = (
        (effective_revenue - total_cost_micro_rub) / total_cost_micro_rub * 100
    )
    result.margin_percent = margin_percent
    result.pricing_margin_percent = pricing_margin_percent

    yesterday = day_start.date()

    if margin_percent < MARGIN_CRITICAL_PERCENT:
        title = (
            f"Negative margin on {yesterday}: revenue={int(total_revenue)}μ₽, "
            f"cost={total_cost_micro_rub}μ₽, margin={margin_percent:.1f}%, "
            f"absorbed={total_absorbed}μ₽"
        )
        await create_alert(
            db,
            severity="critical",
            source="billing",
            title=title,
            details={
                "date": str(yesterday),
                "revenue_micro_rub": int(total_revenue),
                "cost_micro_rub": total_cost_micro_rub,
                "absorbed_micro_rub": total_absorbed,
                "margin_percent": round(margin_percent, 2),
                "pricing_margin_percent": round(pricing_margin_percent, 2),
            },
        )
        result.alerts_created.append(("critical", title))
    elif margin_percent < MARGIN_WARNING_PERCENT:
        if pricing_margin_percent >= PRICING_MARGIN_HEALTHY_PERCENT:
            # Pricing is fine; margin is depressed only by absorbed overdrafts.
            title = (
                f"Margin on {yesterday}: {margin_percent:.1f}% "
                f"(pricing margin: {pricing_margin_percent:.1f}%). "
                f"Absorbed overdrafts: {total_absorbed}μ₽."
            )
            await create_alert(
                db,
                severity="info",
                source="billing",
                title=title,
                details={
                    "date": str(yesterday),
                    "margin_percent": round(margin_percent, 2),
                    "pricing_margin_percent": round(pricing_margin_percent, 2),
                    "absorbed_micro_rub": total_absorbed,
                },
            )
            result.alerts_created.append(("info", title))
        else:
            title = (
                f"Low margin on {yesterday}: {margin_percent:.1f}%. "
                f"Pricing margin: {pricing_margin_percent:.1f}%. "
                "Expected ~25%. Check pricing or exchange rate."
            )
            await create_alert(
                db,
                severity="warning",
                source="billing",
                title=title,
                details={
                    "date": str(yesterday),
                    "margin_percent": round(margin_percent, 2),
                    "pricing_margin_percent": round(pricing_margin_percent, 2),
                },
            )
            result.alerts_created.append(("warning", title))


# ── CHECK 4: orphaned usage logs ────────────────────────────────────────────


async def _check_orphaned_usage_logs(
    db: AsyncSession, result: ReconciliationResult
) -> None:
    day_start, day_end = _yesterday_bounds()

    orphan_count = (
        await db.execute(
            text(
                """
                SELECT COUNT(*)
                FROM usage_logs ul
                LEFT JOIN transactions t
                  ON t.request_id = ul.request_id AND t.type = 'debit'
                WHERE ul.status = 'completed'
                  AND ul.actual_cost_micro_rub > 0
                  AND ul.created_at >= :day_start
                  AND ul.created_at < :day_end
                  AND t.id IS NULL
                """
            ),
            {"day_start": day_start, "day_end": day_end},
        )
    ).scalar() or 0

    result.orphaned_logs = orphan_count
    if orphan_count > 0:
        title = (
            f"{orphan_count} completed usage logs have no matching debit "
            "transaction. Possible billing failure."
        )
        await create_alert(
            db,
            severity="critical",
            source="billing",
            title=title,
            details={"orphan_count": orphan_count},
        )
        result.alerts_created.append(("critical", title))


# ── Summary ─────────────────────────────────────────────────────────────────


def _build_summary_message(result: ReconciliationResult) -> tuple[str, str]:
    """Returns (severity, message) for the Telegram summary."""
    if result.all_passed:
        msg = (
            "Daily reconciliation OK. "
            f"Users={result.users_checked}, transactions={result.transactions_checked}, "
            f"stale_reservations=0, orphaned_logs=0."
        )
        if result.margin_percent is not None:
            msg += f" Margin={result.margin_percent:.1f}%."
        return "info", msg

    has_critical = any(sev == "critical" for sev, _ in result.alerts_created)
    severity = "critical" if has_critical else "warning"
    parts: list[str] = [
        "Reconciliation issues detected:",
        f"balance_mismatches={result.balance_mismatches}",
        f"stale_reservations={result.stale_reservations}",
        f"orphaned_logs={result.orphaned_logs}",
    ]
    if result.margin_percent is not None:
        parts.append(f"margin={result.margin_percent:.1f}%")
    return severity, " ".join(parts)


# ── Public entry point ──────────────────────────────────────────────────────


async def run_daily_reconciliation(
    db: AsyncSession | None = None,
) -> ReconciliationResult:
    """Run the four §11.1 checks and dispatch a Telegram summary.

    When `db` is None (cron invocation), opens its own session from the
    factory. When called from a test, the provided session is used so the
    test can inspect rows in the same transaction.
    """
    if db is None:
        factory = get_session_factory()
        async with factory() as session:
            result = await _run_with_session(session)
            return result
    return await _run_with_session(db)


async def _run_with_session(db: AsyncSession) -> ReconciliationResult:
    result = ReconciliationResult()

    await _check_balance_invariant(db, result)
    await _check_stale_reservations(db, result)
    await _check_revenue_vs_cost(db, result)
    await _check_orphaned_usage_logs(db, result)

    # Commit the alerts created above. create_alert only flushes — the caller
    # owns the transaction (per the existing Task 9.3 contract).
    await db.commit()

    severity, message = _build_summary_message(result)
    await send_telegram_alert(message, severity=severity)

    logger.info(
        "Reconciliation done: balance_ok=%s mismatches=%d stale=%d orphans=%d margin=%s",
        result.balance_check_passed,
        result.balance_mismatches,
        result.stale_reservations,
        result.orphaned_logs,
        f"{result.margin_percent:.2f}%" if result.margin_percent is not None else "n/a",
    )
    return result


async def _main() -> None:
    """Entry point for `python -m app.tasks.reconciliation`.

    Initializes the DB engine (only done by FastAPI startup in normal serving)
    and disposes of it on exit so this can be run cleanly from cron.
    """
    init_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )
    try:
        await run_daily_reconciliation()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_main())
