"""Cleanup cron — SPEC.md §13.6.

Two responsibilities, run on different schedules:

1. `release_expired_reservations()` runs every minute. Marks reservations
   still 'active' after 5 minutes as 'expired', refunds the user's
   reserved_micro_rub, files a warning alert.
2. `run_data_retention()` runs daily at 04:00 UTC. Purges old rows per
   SCHEMA.md §6:
   - expired refresh tokens
   - password reset tokens older than 24 hours
   - notifications older than 90 days
   - alerts older than 365 days
   - soft-deleted conversations older than 30 days (CASCADEs to messages)
   - usage_logs older than 365 days
   - admin_audit_log older than 3 years
   - finalized/released/expired reservations older than 90 days

Module CLI selects which subset to run:
    docker compose exec backend python -m app.tasks.cleanup reservations
    docker compose exec backend python -m app.tasks.cleanup retention
    docker compose exec backend python -m app.tasks.cleanup all   # default
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.database import dispose_engine, get_session_factory, init_engine
from app.models.alert import Alert
from app.models.reservation import Reservation
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services.openrouter_cost import fetch_generation_cost
from app.services.openrouter_proxy import get_active_key

logger = logging.getLogger(__name__)


# ── Reservation cleanup (every-minute job) ──────────────────────────────────


async def _expire_one(db: AsyncSession, reservation_id: uuid.UUID) -> bool:
    """Expire a single reservation, but ONLY if it is still 'active'.

    Locks the reservation row FOR UPDATE and re-checks its status under that lock
    before releasing the hold — the same guard finalize_reservation uses. This is
    what makes the sweep race-safe (tr-6qf7): a concurrent finalize/release, or a
    second reaper (the in-process loop racing a stray host-cron run), that already
    moved the row out of 'active' is seen here and skipped, so reserved_micro_rub
    is never double-decremented and a finalized/released status is never clobbered
    back to 'expired'. Returns True if it expired the row, False if it skipped.

    Locks the reservation BEFORE the user row, matching finalize_reservation's
    order so the two paths can't deadlock. Runs in a savepoint so one failing row
    doesn't abort the whole batch.
    """
    async with db.begin_nested():
        reservation = (
            await db.execute(
                select(Reservation)
                .where(Reservation.id == reservation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if reservation is None or reservation.status != "active":
            return False

        user = (
            await db.execute(
                select(User).where(User.id == reservation.user_id).with_for_update()
            )
        ).scalar_one()

        user.reserved_micro_rub = max(
            user.reserved_micro_rub - reservation.reserved_micro_rub, 0
        )
        reservation.status = "expired"
        reservation.finalized_at = datetime.now(timezone.utc)

        db.add(
            Alert(
                severity="warning",
                source="billing",
                title="Просроченная резервация автоматически освобождена",
                message=(
                    f"Reservation {reservation.id} for user "
                    f"{reservation.user_id} expired after 5 minutes. "
                    f"Released {reservation.reserved_micro_rub} μ₽."
                ),
                details={
                    "reservation_id": str(reservation.id),
                    "user_id": str(reservation.user_id),
                    "request_id": str(reservation.request_id),
                    "reserved_micro_rub": reservation.reserved_micro_rub,
                    "model_id": reservation.model_id,
                },
            )
        )
    return True


async def release_expired_reservations(db: AsyncSession) -> int:
    """
    Finds all reservations with status='active' and created_at < now() - 5 minutes
    and expires each one (decrement user.reserved_micro_rub, file a warning alert).
    Each row is expired under its own row lock with a status re-check via
    _expire_one, so the sweep is safe to run concurrently with a finalize or a
    second reaper (tr-6qf7). Returns the count of expired reservations processed.
    """
    cutoff = func.now() - text("interval '5 minutes'")
    candidate_ids = (
        await db.execute(
            select(Reservation.id)
            .where(Reservation.status == "active")
            .where(Reservation.created_at < cutoff)
        )
    ).scalars().all()

    count = 0
    for reservation_id in candidate_ids:
        try:
            if await _expire_one(db, reservation_id):
                count += 1
        except Exception:
            # Savepoint already rolled back; skip this reservation and continue.
            continue

    await db.commit()
    return count


# ── Partial-cost reconciliation (every-minute job, tr-sgz) ──────────────────


async def reconcile_partial_costs(db: AsyncSession) -> int:
    """Fill openrouter_cost_micro_usd for usage_logs whose cost is UNKNOWN.

    Two cases share one mechanism — a /generation cost lookup that ONLY reads the
    cost basis and never touches the user charge or balance:

      • Stop-aborted `partial` rows whose cost never arrived in a usage chunk
        (cost still 0) — the original tr-sgz case.
      • `completed` rows flagged `cost_pending` because OpenRouter omitted
        `usage.cost` from the stream even though the request finished (tr-34mj).
        On success the flag is cleared so the row is no longer "unknown"; a
        legitimate $0 (free model) is never flagged, so it is never re-queried.

    Candidates additionally require a generation id and an age of 10s–24h (give
    OpenRouter time to finalize; stop retrying rows it will never return).
    Returns the count updated.
    """
    candidates = (
        await db.execute(
            select(UsageLog)
            .where(
                or_(
                    and_(
                        UsageLog.status == "partial",
                        UsageLog.openrouter_cost_micro_usd == 0,
                    ),
                    UsageLog.cost_pending.is_(True),
                )
            )
            .where(UsageLog.openrouter_request_id.is_not(None))
            .where(UsageLog.created_at < func.now() - text("interval '10 seconds'"))
            .where(UsageLog.created_at > func.now() - text("interval '24 hours'"))
        )
    ).scalars().all()

    if not candidates:
        return 0

    try:
        api_key = await get_active_key(db)
    except Exception:
        logger.warning("reconcile: no active OpenRouter key; skipping this run")
        return 0

    count = 0
    for row in candidates:
        try:
            cost = await fetch_generation_cost(api_key, row.openrouter_request_id)
            if cost is None:
                continue
            row.openrouter_cost_micro_usd = cost
            # Cost is now known (even a confirmed 0): clear the pending flag so the
            # row leaves the candidate set and counts correctly in margin (tr-34mj).
            row.cost_pending = False
            count += 1
        except Exception:
            logger.warning("reconcile: failed for usage_log %s", row.id, exc_info=True)
            continue

    await db.commit()
    return count


# ── Reserved-counter drift reconciliation (every-minute job, tr-12jl) ────────


async def reconcile_reserved_drift(db: AsyncSession) -> int:
    """Heal users whose denormalised users.reserved_micro_rub diverges from the
    live SUM of their active reservations (tr-12jl).

    reserved_micro_rub is a denorm of SUM(reserved WHERE status='active'), and the
    available-balance check in create_reservation trusts it. If it drifts ABOVE the
    live sum — a phantom hold with no backing active row — the user is falsely
    blocked from their own balance with no way to recover. This recomputes the
    counter under the user-row lock and files a warning alert per correction, so a
    recurring drift source stays visible instead of being silently papered over.
    Returns the count of users healed.
    """
    active_sum = (
        select(
            Reservation.user_id.label("user_id"),
            func.sum(Reservation.reserved_micro_rub).label("active_sum"),
        )
        .where(Reservation.status == "active")
        .group_by(Reservation.user_id)
        .subquery()
    )
    drifting = (
        await db.execute(
            select(User.id)
            .outerjoin(active_sum, active_sum.c.user_id == User.id)
            .where(
                User.reserved_micro_rub != func.coalesce(active_sum.c.active_sum, 0)
            )
        )
    ).scalars().all()

    count = 0
    for user_id in drifting:
        try:
            async with db.begin_nested():
                # populate_existing forces this locked read to overwrite any
                # cached instance, so we always reconcile against the fresh,
                # committed reserved value (mirrors create_reservation's lock).
                user = (
                    await db.execute(
                        select(User)
                        .where(User.id == user_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                # SUM(bigint) comes back as Decimal in Postgres — coerce to int so
                # the value assigns cleanly to the BIGINT column and stays JSON
                # serialisable for the alert details below.
                live_sum = int(
                    (
                        await db.execute(
                            select(
                                func.coalesce(func.sum(Reservation.reserved_micro_rub), 0)
                            )
                            .where(Reservation.user_id == user_id)
                            .where(Reservation.status == "active")
                        )
                    ).scalar_one()
                )
                if user.reserved_micro_rub == live_sum:
                    continue  # raced — became consistent before we locked the row

                old_reserved = user.reserved_micro_rub
                drift = old_reserved - live_sum
                user.reserved_micro_rub = live_sum

                db.add(
                    Alert(
                        severity="warning",
                        source="billing",
                        title="Дрейф reserved_micro_rub скорректирован",
                        message=(
                            f"User {user_id}: reserved_micro_rub {old_reserved} → "
                            f"{live_sum} (drift {drift} μ₽) reconciled to "
                            f"SUM(active reservations)."
                        ),
                        details={
                            "kind": "reserved_drift_corrected",
                            "user_id": str(user_id),
                            "old_reserved_micro_rub": old_reserved,
                            "new_reserved_micro_rub": live_sum,
                            "drift_micro_rub": drift,
                        },
                    )
                )
            count += 1
        except Exception:
            # Savepoint already rolled back; skip this user but log it — a silent
            # skip here would hide a recurring reconciliation failure.
            logger.warning(
                "reserved-drift reconcile failed for user %s", user_id, exc_info=True
            )
            continue

    await db.commit()
    return count


# ── Daily data retention purge — SCHEMA.md §6 ───────────────────────────────


@dataclass
class RetentionResult:
    """Row counts deleted per category. `transactions`, `model_pricing`,
    and `api_keys` have Forever retention and are intentionally absent."""

    refresh_tokens: int = 0
    password_reset_tokens: int = 0
    notifications: int = 0
    alerts: int = 0
    conversations: int = 0  # messages CASCADE-deleted with their conversation
    usage_logs: int = 0
    admin_audit_log: int = 0
    reservations: int = 0  # finalized/released/expired > 90d


async def run_data_retention(db: AsyncSession) -> RetentionResult:
    """Run all daily retention purges per SCHEMA.md §6.

    Each DELETE runs as a separate statement and returns the affected
    rowcount. We commit once at the end so the whole run is atomic.
    """
    result = RetentionResult()

    # Expired refresh tokens. (Tokens self-expire by expires_at — no need to
    # check status.)
    r = await db.execute(text("DELETE FROM refresh_tokens WHERE expires_at < now()"))
    result.refresh_tokens = r.rowcount or 0

    # Password reset tokens older than 24 hours. §6 deletes ALL such rows
    # regardless of used_at — tokens auto-expire after 1 hour anyway, so
    # anything 24h old is dead. (The model has used_at TIMESTAMPTZ, not a
    # boolean — see app/models/password_reset_token.py.)
    r = await db.execute(
        text(
            "DELETE FROM password_reset_tokens "
            "WHERE created_at < now() - interval '24 hours'"
        )
    )
    result.password_reset_tokens = r.rowcount or 0

    # Old notifications.
    r = await db.execute(
        text("DELETE FROM notifications WHERE created_at < now() - interval '90 days'")
    )
    result.notifications = r.rowcount or 0

    # Old alerts.
    r = await db.execute(
        text("DELETE FROM alerts WHERE created_at < now() - interval '365 days'")
    )
    result.alerts = r.rowcount or 0

    # Soft-deleted conversations past the 30-day recovery window.
    # messages.conversation_id has ON DELETE CASCADE, so child messages go
    # with them automatically (verified in app/models/message.py).
    r = await db.execute(
        text(
            "DELETE FROM conversations "
            "WHERE deleted_at IS NOT NULL "
            "  AND deleted_at < now() - interval '30 days'"
        )
    )
    result.conversations = r.rowcount or 0

    # Old usage_logs.
    r = await db.execute(
        text("DELETE FROM usage_logs WHERE created_at < now() - interval '365 days'")
    )
    result.usage_logs = r.rowcount or 0

    # Old admin_audit_log.
    r = await db.execute(
        text(
            "DELETE FROM admin_audit_log "
            "WHERE created_at < now() - interval '3 years'"
        )
    )
    result.admin_audit_log = r.rowcount or 0

    # Finalized/released/expired reservations older than 90 days (SCHEMA.md
    # §6: reservations retention = 90 days after finalization).
    r = await db.execute(
        text(
            "DELETE FROM reservations "
            "WHERE status IN ('finalized', 'released', 'expired') "
            "  AND finalized_at < now() - interval '90 days'"
        )
    )
    result.reservations = r.rowcount or 0

    await db.commit()

    logger.info(
        "Data retention complete — "
        "refresh_tokens=%d, reset_tokens=%d, notifications=%d, alerts=%d, "
        "conversations=%d, usage_logs=%d, audit_log=%d, reservations=%d",
        result.refresh_tokens,
        result.password_reset_tokens,
        result.notifications,
        result.alerts,
        result.conversations,
        result.usage_logs,
        result.admin_audit_log,
        result.reservations,
    )

    return result


# ── Standalone entry point ─────────────────────────────────────────────────


async def _run_reservations() -> None:
    factory = get_session_factory()
    async with factory() as session:
        released = await release_expired_reservations(session)
        logger.info("Released %d expired reservations", released)


async def _run_retention() -> None:
    factory = get_session_factory()
    async with factory() as session:
        await run_data_retention(session)


async def _run_reconcile() -> None:
    factory = get_session_factory()
    async with factory() as session:
        updated = await reconcile_partial_costs(session)
        logger.info("Reconciled %d partial costs", updated)


async def _main(job: str) -> None:
    """Entry for `python -m app.tasks.cleanup [reservations|retention|reconcile|all]`.

    Cron schedule (SPEC.md §13.6 + Task 9.6):
    - every minute → `cleanup.py reservations` (cheap reservation sweep)
    - every minute → `cleanup.py reconcile` (fill OpenRouter cost for partial rows)
    - daily 04:00 UTC → `cleanup.py retention` (heavy retention purge)
    - `all` (default) runs all three — useful for ad-hoc invocations.
    """
    init_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )
    try:
        if job in ("reservations", "all"):
            await _run_reservations()
        if job in ("retention", "all"):
            await _run_retention()
        if job in ("reconcile", "all"):
            await _run_reconcile()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Trinity cleanup cron")
    parser.add_argument(
        "job",
        nargs="?",
        default="all",
        choices=["reservations", "retention", "reconcile", "all"],
        help="Which sub-job to run (default: all).",
    )
    args = parser.parse_args()
    asyncio.run(_main(args.job))
