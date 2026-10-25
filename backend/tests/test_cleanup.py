import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy import delete as sa_delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.tasks.cleanup as cleanup_mod
from app.core.security import hash_password
from app.models.alert import Alert
from app.models.audit_log import AdminAuditLog
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.notification import Notification
from app.models.password_reset_token import PasswordResetToken
from app.models.refresh_token import RefreshToken
from app.models.reservation import Reservation
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services.billing_service import (
    create_reservation,
    credit_balance,
    finalize_reservation,
    release_reservation,
)
from app.tasks.cleanup import reconcile_partial_costs, release_expired_reservations, run_data_retention

TEST_EMAIL = "test_cleanup@example.com"


async def _purge(session: AsyncSession, email: str) -> None:
    existing = (
        await session.execute(select(User).where(User.email_lower == email.lower()))
    ).scalar_one_or_none()
    if existing is None:
        return
    user_id = existing.id
    # Alerts created by cleanup reference reservation/user in details JSONB only,
    # so purge them by source/created_at against this user via JSONB lookup.
    await session.execute(
        sa_delete(Alert).where(
            Alert.source == "billing",
            Alert.details["user_id"].astext == str(user_id),
        )
    )
    await session.execute(sa_delete(UsageLog).where(UsageLog.user_id == user_id))
    await session.execute(sa_delete(Reservation).where(Reservation.user_id == user_id))
    await session.execute(sa_delete(Transaction).where(Transaction.user_id == user_id))
    await session.execute(sa_delete(User).where(User.id == user_id))
    await session.commit()


@pytest_asyncio.fixture
async def cleanup_user(db_session: AsyncSession):
    await _purge(db_session, TEST_EMAIL)

    user = User(
        email=TEST_EMAIL,
        display_name="Очистка Тест",
        password_hash=None,
        role="user",
        status="active",
        balance_micro_rub=0,
        reserved_micro_rub=0,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    yield user

    db_session.expire_all()
    await _purge(db_session, TEST_EMAIL)


async def _seed_balance(session: AsyncSession, user_id, amount: int, key: str) -> None:
    await credit_balance(
        session,
        user_id=user_id,
        amount_micro_rub=amount,
        idempotency_key=key,
        description="Test seed top-up",
    )
    await session.commit()


async def _age_reservation(session: AsyncSession, reservation_id: uuid.UUID, minutes: int) -> None:
    """Backdate created_at on a reservation by N minutes."""
    await session.execute(
        text(
            "UPDATE reservations "
            "SET created_at = now() - (:m || ' minutes')::interval "
            "WHERE id = :rid"
        ),
        {"m": str(minutes), "rid": reservation_id},
    )
    await session.commit()


async def test_expired_reservation_is_released(
    db_session: AsyncSession, cleanup_user: User
) -> None:
    user_id = cleanup_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "cl-exp")

    reservation = await create_reservation(
        db_session,
        user_id=user_id,
        estimated_cost_micro_rub=100_000_000,
        model_id="openai/gpt-4o",
    )
    reservation_id = reservation.id
    reserved_amount = reservation.reserved_micro_rub
    await db_session.commit()

    await _age_reservation(db_session, reservation_id, 6)

    count = await release_expired_reservations(db_session)
    assert count == 1

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.reserved_micro_rub == 0

    res = (
        await db_session.execute(select(Reservation).where(Reservation.id == reservation_id))
    ).scalar_one()
    assert res.status == "expired"
    assert res.finalized_at is not None

    alert = (
        await db_session.execute(
            select(Alert).where(
                Alert.source == "billing",
                Alert.details["reservation_id"].astext == str(reservation_id),
            )
        )
    ).scalar_one()
    assert alert.severity == "warning"
    assert str(reserved_amount) in alert.message or str(user_id) in alert.message


async def test_recent_reservation_is_untouched(
    db_session: AsyncSession, cleanup_user: User
) -> None:
    user_id = cleanup_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "cl-recent")

    reservation = await create_reservation(
        db_session,
        user_id=user_id,
        estimated_cost_micro_rub=100_000_000,
        model_id="openai/gpt-4o",
    )
    reservation_id = reservation.id
    reserved_amount = reservation.reserved_micro_rub
    await db_session.commit()

    await _age_reservation(db_session, reservation_id, 4)

    count = await release_expired_reservations(db_session)
    assert count == 0

    db_session.expire_all()
    res = (
        await db_session.execute(select(Reservation).where(Reservation.id == reservation_id))
    ).scalar_one()
    assert res.status == "active"

    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.reserved_micro_rub == reserved_amount


async def test_finalized_reservation_is_untouched(
    db_session: AsyncSession, cleanup_user: User
) -> None:
    user_id = cleanup_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "cl-fin")

    reservation = await create_reservation(
        db_session,
        user_id=user_id,
        estimated_cost_micro_rub=100_000_000,
        model_id="openai/gpt-4o",
    )
    reservation_id = reservation.id
    await db_session.commit()

    await finalize_reservation(
        db_session,
        reservation_id=reservation_id,
        actual_cost_micro_rub=80_000_000,
        prompt_tokens=10,
        completion_tokens=10,
        model_id="openai/gpt-4o",
    )
    await db_session.commit()

    await _age_reservation(db_session, reservation_id, 10)

    count = await release_expired_reservations(db_session)
    assert count == 0

    db_session.expire_all()
    res = (
        await db_session.execute(select(Reservation).where(Reservation.id == reservation_id))
    ).scalar_one()
    assert res.status == "finalized"


async def test_released_reservation_is_untouched(
    db_session: AsyncSession, cleanup_user: User
) -> None:
    user_id = cleanup_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "cl-rel")

    reservation = await create_reservation(
        db_session,
        user_id=user_id,
        estimated_cost_micro_rub=100_000_000,
        model_id="openai/gpt-4o",
    )
    reservation_id = reservation.id
    await db_session.commit()

    await release_reservation(db_session, reservation_id=reservation_id)
    await db_session.commit()

    await _age_reservation(db_session, reservation_id, 10)

    count = await release_expired_reservations(db_session)
    assert count == 0

    db_session.expire_all()
    res = (
        await db_session.execute(select(Reservation).where(Reservation.id == reservation_id))
    ).scalar_one()
    assert res.status == "released"


async def test_already_expired_not_double_processed(
    db_session: AsyncSession, cleanup_user: User
) -> None:
    user_id = cleanup_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "cl-dbl")

    reservation = await create_reservation(
        db_session,
        user_id=user_id,
        estimated_cost_micro_rub=100_000_000,
        model_id="openai/gpt-4o",
    )
    reservation_id = reservation.id
    await db_session.commit()

    await _age_reservation(db_session, reservation_id, 6)

    count_first = await release_expired_reservations(db_session)
    assert count_first == 1

    # Second run: reservation is now 'expired', not 'active' → not matched
    count_second = await release_expired_reservations(db_session)
    assert count_second == 0

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    # reserved_micro_rub should be 0, not negative (no double-decrement)
    assert user.reserved_micro_rub == 0

    # Only one alert created
    alert_count = (
        await db_session.execute(
            select(func.count())
            .select_from(Alert)
            .where(
                Alert.source == "billing",
                Alert.details["reservation_id"].astext == str(reservation_id),
            )
        )
    ).scalar()
    assert alert_count == 1


async def test_multiple_expired_processed_in_one_run(
    db_session: AsyncSession, cleanup_user: User
) -> None:
    user_id = cleanup_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "cl-multi")

    reservation_ids = []
    total_reserved = 0
    for i in range(3):
        r = await create_reservation(
            db_session,
            user_id=user_id,
            estimated_cost_micro_rub=50_000_000,
            model_id="openai/gpt-4o",
        )
        reservation_ids.append(r.id)
        total_reserved += r.reserved_micro_rub
        await db_session.commit()

    # Backdate all three
    for rid in reservation_ids:
        await _age_reservation(db_session, rid, 6)

    count = await release_expired_reservations(db_session)
    assert count == 3

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.reserved_micro_rub == 0

    for rid in reservation_ids:
        res = (
            await db_session.execute(select(Reservation).where(Reservation.id == rid))
        ).scalar_one()
        assert res.status == "expired"


async def test_balance_unchanged_after_cleanup(
    db_session: AsyncSession, cleanup_user: User
) -> None:
    user_id = cleanup_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "cl-bal")

    reservation = await create_reservation(
        db_session,
        user_id=user_id,
        estimated_cost_micro_rub=100_000_000,
        model_id="openai/gpt-4o",
    )
    reservation_id = reservation.id
    await db_session.commit()

    await _age_reservation(db_session, reservation_id, 6)

    count = await release_expired_reservations(db_session)
    assert count == 1

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub == 1_000_000_000
    assert user.reserved_micro_rub == 0


async def test_expire_one_is_idempotent_on_second_call(
    db_session: AsyncSession, cleanup_user: User
) -> None:
    """tr-6qf7: a second runner (e.g. a stray host-cron run racing the in-process
    60s loop) that calls _expire_one on an already-expired reservation must be a
    no-op — no second decrement of reserved_micro_rub, no second release alert.
    The guarded status re-check under the row lock is what makes this safe.
    """
    user_id = cleanup_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "cl-idem")
    r = await create_reservation(
        db_session, user_id=user_id, estimated_cost_micro_rub=100_000_000, model_id="openai/gpt-4o"
    )
    await db_session.commit()
    rid = r.id

    first = await cleanup_mod._expire_one(db_session, rid)
    await db_session.commit()
    assert first is True

    second = await cleanup_mod._expire_one(db_session, rid)
    await db_session.commit()
    assert second is False  # already 'expired' — the guard skips it

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.reserved_micro_rub == 0  # decremented exactly once, never twice

    alert_count = (
        await db_session.execute(
            select(func.count())
            .select_from(Alert)
            .where(
                Alert.source == "billing",
                Alert.details["reservation_id"].astext == str(rid),
            )
        )
    ).scalar()
    assert alert_count == 1


async def test_expire_one_skips_concurrently_finalized_reservation(
    db_session: AsyncSession, cleanup_user: User, engine
) -> None:
    """tr-6qf7: if a reservation is finalized by a concurrent request after the
    reaper picked it as a candidate, _expire_one must re-read the status under the
    row lock and SKIP it — no double decrement of reserved_micro_rub and no
    clobber of the 'finalized' status back to 'expired'.
    """
    user_id = cleanup_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "cl-finrace")
    # R1 is the reaper's target; R2 keeps reserved non-zero so a stray
    # double-decrement of R1 is visible (not hidden by the >= 0 clamp).
    r1 = await create_reservation(
        db_session, user_id=user_id, estimated_cost_micro_rub=100_000_000, model_id="openai/gpt-4o"
    )
    r2 = await create_reservation(
        db_session, user_id=user_id, estimated_cost_micro_rub=100_000_000, model_id="openai/gpt-4o"
    )
    await db_session.commit()
    rid, r2_reserved = r1.id, r2.reserved_micro_rub

    # A concurrent request finalizes R1 on a separate session/connection — the
    # exact state change the reaper must notice between candidate-scan and expiry.
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as other:
        await finalize_reservation(
            other,
            reservation_id=rid,
            actual_cost_micro_rub=40_000_000,
            prompt_tokens=10,
            completion_tokens=10,
            model_id="openai/gpt-4o",
        )
        await other.commit()

    db_session.expire_all()
    expired = await cleanup_mod._expire_one(db_session, rid)
    await db_session.commit()
    assert expired is False  # guard saw 'finalized', refused to act

    db_session.expire_all()
    r1_row = (
        await db_session.execute(select(Reservation).where(Reservation.id == rid))
    ).scalar_one()
    assert r1_row.status == "finalized"  # NOT clobbered to 'expired'
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    # finalize already released R1's hold once; the reaper must not release again.
    assert user.reserved_micro_rub == r2_reserved

    alert_count = (
        await db_session.execute(
            select(func.count())
            .select_from(Alert)
            .where(
                Alert.source == "billing",
                Alert.details["reservation_id"].astext == str(rid),
            )
        )
    ).scalar()
    assert alert_count == 0  # no release alert for a reservation it didn't expire


async def test_reconcile_reserved_drift_heals_phantom_hold(
    db_session: AsyncSession, cleanup_user: User, engine
) -> None:
    """tr-12jl: a user whose reserved_micro_rub exceeds SUM(active reservations)
    — a phantom hold with no backing row — is healed back to the live sum, the
    genuine active hold is kept, and a warning alert records the correction."""
    user_id = cleanup_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "cl-drift")
    # One genuine active reservation — its hold must survive reconciliation.
    r = await create_reservation(
        db_session, user_id=user_id, estimated_cost_micro_rub=100_000_000, model_id="openai/gpt-4o"
    )
    await db_session.commit()
    genuine = r.reserved_micro_rub

    # Inject a phantom hold: bump reserved with NO backing active reservation row.
    phantom = 703_534
    await db_session.execute(
        text("UPDATE users SET reserved_micro_rub = reserved_micro_rub + :p WHERE id = :id"),
        {"p": phantom, "id": user_id},
    )
    await db_session.commit()

    # Reconcile on a FRESH session, as the in-process cleanup loop does each tick.
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as recon_db:
        healed = await cleanup_mod.reconcile_reserved_drift(recon_db)
    assert healed == 1

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.reserved_micro_rub == genuine  # phantom removed, genuine hold kept

    alert = (
        await db_session.execute(
            select(Alert).where(
                Alert.source == "billing",
                Alert.details["kind"].astext == "reserved_drift_corrected",
                Alert.details["user_id"].astext == str(user_id),
            )
        )
    ).scalar_one()
    assert alert.severity == "warning"
    assert alert.details["drift_micro_rub"] == phantom


async def test_reconcile_reserved_drift_is_noop_when_consistent(
    db_session: AsyncSession, cleanup_user: User
) -> None:
    """When reserved_micro_rub already equals SUM(active reservations),
    reconciliation changes nothing and files no alert."""
    user_id = cleanup_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "cl-nodrift")
    r = await create_reservation(
        db_session, user_id=user_id, estimated_cost_micro_rub=100_000_000, model_id="openai/gpt-4o"
    )
    await db_session.commit()
    before = r.reserved_micro_rub

    healed = await cleanup_mod.reconcile_reserved_drift(db_session)
    assert healed == 0

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.reserved_micro_rub == before

    alert_count = (
        await db_session.execute(
            select(func.count())
            .select_from(Alert)
            .where(
                Alert.source == "billing",
                Alert.details["kind"].astext == "reserved_drift_corrected",
                Alert.details["user_id"].astext == str(user_id),
            )
        )
    ).scalar()
    assert alert_count == 0


# ── Data retention tests (Task 9.5 — SCHEMA.md §6) ──────────────────────────


RETENTION_EMAIL = "test_retention@example.com"


async def _purge_retention_user(session: AsyncSession) -> None:
    existing = (
        await session.execute(
            select(User).where(User.email_lower == RETENTION_EMAIL.lower())
        )
    ).scalar_one_or_none()
    if existing is None:
        return
    uid = existing.id
    # Children that reference the user — clear before deleting the user.
    await session.execute(
        sa_delete(Message).where(
            Message.conversation_id.in_(
                select(Conversation.id).where(Conversation.user_id == uid)
            )
        )
    )
    await session.execute(sa_delete(Conversation).where(Conversation.user_id == uid))
    await session.execute(sa_delete(Notification).where(Notification.user_id == uid))
    await session.execute(sa_delete(UsageLog).where(UsageLog.user_id == uid))
    await session.execute(sa_delete(Reservation).where(Reservation.user_id == uid))
    await session.execute(sa_delete(Transaction).where(Transaction.user_id == uid))
    await session.execute(sa_delete(RefreshToken).where(RefreshToken.user_id == uid))
    await session.execute(
        sa_delete(PasswordResetToken).where(PasswordResetToken.user_id == uid)
    )
    await session.execute(
        sa_delete(AdminAuditLog).where(AdminAuditLog.actor_id == uid)
    )
    await session.execute(sa_delete(User).where(User.id == uid))
    await session.commit()


@pytest_asyncio.fixture
async def retention_user(db_session: AsyncSession):
    await _purge_retention_user(db_session)
    user = User(
        email=RETENTION_EMAIL,
        display_name="Retention",
        password_hash=await hash_password("Password1"),
        role="user",
        status="active",
        balance_micro_rub=0,
        reserved_micro_rub=0,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    yield user
    db_session.expire_all()
    await _purge_retention_user(db_session)


def _ago(days: int = 0, hours: int = 0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days, hours=hours)


async def _backdate(
    session: AsyncSession, table: str, row_id: uuid.UUID, column: str, when: datetime
) -> None:
    await session.execute(
        text(f"UPDATE {table} SET {column} = :when WHERE id = :id"),
        {"when": when, "id": row_id},
    )
    await session.commit()


# CHECK: expired refresh token deleted, fresh kept


async def test_retention_expired_refresh_token_deleted(
    db_session: AsyncSession, retention_user: User
) -> None:
    expired = RefreshToken(
        user_id=retention_user.id,
        token_hash=f"hash-expired-{uuid.uuid4().hex}",
        expires_at=_ago(hours=1),
        absolute_expires_at=_ago(hours=1),
    )
    fresh = RefreshToken(
        user_id=retention_user.id,
        token_hash=f"hash-fresh-{uuid.uuid4().hex}",
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        absolute_expires_at=datetime.now(timezone.utc) + timedelta(days=14),
    )
    db_session.add_all([expired, fresh])
    await db_session.commit()
    expired_id, fresh_id = expired.id, fresh.id

    result = await run_data_retention(db_session)

    assert result.refresh_tokens >= 1
    remaining = (
        await db_session.execute(
            select(RefreshToken).where(RefreshToken.id.in_([expired_id, fresh_id]))
        )
    ).scalars().all()
    remaining_ids = {r.id for r in remaining}
    assert expired_id not in remaining_ids
    assert fresh_id in remaining_ids


# CHECK: password reset token >24h deleted, fresh kept


async def test_retention_old_password_reset_token_deleted(
    db_session: AsyncSession, retention_user: User
) -> None:
    old = PasswordResetToken(
        user_id=retention_user.id,
        token_hash=f"prt-old-{uuid.uuid4().hex}",
        expires_at=_ago(hours=23),  # past, but the rule is created_at-based
    )
    new = PasswordResetToken(
        user_id=retention_user.id,
        token_hash=f"prt-new-{uuid.uuid4().hex}",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add_all([old, new])
    await db_session.commit()
    old_id, new_id = old.id, new.id
    # Backdate created_at on the old token to 25h ago.
    await _backdate(db_session, "password_reset_tokens", old_id, "created_at", _ago(hours=25))

    result = await run_data_retention(db_session)

    assert result.password_reset_tokens >= 1
    remaining = (
        await db_session.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.id.in_([old_id, new_id])
            )
        )
    ).scalars().all()
    ids = {r.id for r in remaining}
    assert old_id not in ids
    assert new_id in ids


# CHECK: notification >90 days deleted, fresh kept


async def test_retention_old_notification_deleted(
    db_session: AsyncSession, retention_user: User
) -> None:
    old = Notification(
        user_id=retention_user.id,
        type="low_balance",
        title="старое",
        message="x",
        channel="in_app",
    )
    fresh = Notification(
        user_id=retention_user.id,
        type="low_balance",
        title="свежее",
        message="x",
        channel="in_app",
    )
    db_session.add_all([old, fresh])
    await db_session.commit()
    old_id, fresh_id = old.id, fresh.id
    await _backdate(db_session, "notifications", old_id, "created_at", _ago(days=91))

    result = await run_data_retention(db_session)

    assert result.notifications >= 1
    remaining_ids = {
        r.id for r in (
            await db_session.execute(
                select(Notification).where(Notification.id.in_([old_id, fresh_id]))
            )
        ).scalars().all()
    }
    assert old_id not in remaining_ids
    assert fresh_id in remaining_ids


# CHECK: alert >365 days deleted, fresh kept


async def test_retention_old_alert_deleted(
    db_session: AsyncSession,
) -> None:
    old = Alert(
        severity="info",
        source="billing",
        title="retention-old-alert",
        message="m",
        details={"tag": "retention-test-old"},
    )
    fresh = Alert(
        severity="info",
        source="billing",
        title="retention-fresh-alert",
        message="m",
        details={"tag": "retention-test-fresh"},
    )
    db_session.add_all([old, fresh])
    await db_session.commit()
    old_id, fresh_id = old.id, fresh.id
    await _backdate(db_session, "alerts", old_id, "created_at", _ago(days=366))

    result = await run_data_retention(db_session)

    assert result.alerts >= 1
    remaining_ids = {
        r.id for r in (
            await db_session.execute(
                select(Alert).where(Alert.id.in_([old_id, fresh_id]))
            )
        ).scalars().all()
    }
    assert old_id not in remaining_ids
    assert fresh_id in remaining_ids


# CHECK: soft-deleted conversation >30 days → hard delete + messages CASCADE


async def test_retention_soft_deleted_conversation_hard_purged_with_messages(
    db_session: AsyncSession, retention_user: User
) -> None:
    old_conv = Conversation(
        user_id=retention_user.id,
        model_id="openai/gpt-4o",
        deleted_at=_ago(days=31),
    )
    fresh_conv = Conversation(
        user_id=retention_user.id,
        model_id="openai/gpt-4o",
        # Either non-deleted OR deleted within the 30-day recovery window.
        deleted_at=None,
    )
    db_session.add_all([old_conv, fresh_conv])
    await db_session.commit()
    old_id, fresh_id = old_conv.id, fresh_conv.id

    msg = Message(
        conversation_id=old_id,
        role="user",
        content="should be cascaded away",
    )
    db_session.add(msg)
    await db_session.commit()
    msg_id = msg.id

    result = await run_data_retention(db_session)

    assert result.conversations >= 1
    convs = {
        c.id for c in (
            await db_session.execute(
                select(Conversation).where(Conversation.id.in_([old_id, fresh_id]))
            )
        ).scalars().all()
    }
    assert old_id not in convs
    assert fresh_id in convs

    # CASCADE: the message should be gone too.
    msg_after = (
        await db_session.execute(select(Message).where(Message.id == msg_id))
    ).scalar_one_or_none()
    assert msg_after is None


# CHECK: usage_log >365 days deleted, fresh kept


async def test_retention_old_usage_log_deleted(
    db_session: AsyncSession, retention_user: User
) -> None:
    def _mk(suffix: str) -> UsageLog:
        return UsageLog(
            user_id=retention_user.id,
            request_id=uuid.uuid4(),
            model_id="openai/gpt-4o",
            input_tokens=10,
            output_tokens=10,
            estimated_input_tokens=10,
            estimated_output_tokens=10,
            estimated_cost_micro_rub=1_000,
            actual_cost_micro_rub=1_000,
            reserved_micro_rub=2_000,
            overdraft_absorbed_micro_rub=0,
            openrouter_cost_micro_usd=10,
            exchange_rate_micro_rub=82_500_000,
            status="completed",
        )

    old, fresh = _mk("old"), _mk("fresh")
    db_session.add_all([old, fresh])
    await db_session.commit()
    old_id, fresh_id = old.id, fresh.id
    await _backdate(db_session, "usage_logs", old_id, "created_at", _ago(days=366))

    result = await run_data_retention(db_session)

    assert result.usage_logs >= 1
    ids = {
        r.id for r in (
            await db_session.execute(
                select(UsageLog).where(UsageLog.id.in_([old_id, fresh_id]))
            )
        ).scalars().all()
    }
    assert old_id not in ids
    assert fresh_id in ids


# CHECK: admin_audit_log >3 years deleted, fresh kept


async def test_retention_old_audit_log_deleted(
    db_session: AsyncSession, retention_user: User
) -> None:
    old = AdminAuditLog(
        actor_id=retention_user.id,
        actor_email=retention_user.email,
        action="retention.test.old",
    )
    fresh = AdminAuditLog(
        actor_id=retention_user.id,
        actor_email=retention_user.email,
        action="retention.test.fresh",
    )
    db_session.add_all([old, fresh])
    await db_session.commit()
    old_id, fresh_id = old.id, fresh.id
    # 3y + 1d ago so the "interval '3 years'" comparison triggers.
    await _backdate(db_session, "admin_audit_log", old_id, "created_at", _ago(days=3 * 365 + 1))

    result = await run_data_retention(db_session)

    assert result.admin_audit_log >= 1
    ids = {
        r.id for r in (
            await db_session.execute(
                select(AdminAuditLog).where(AdminAuditLog.id.in_([old_id, fresh_id]))
            )
        ).scalars().all()
    }
    assert old_id not in ids
    assert fresh_id in ids


# CHECK: transactions table is never touched


async def test_retention_does_not_touch_transactions(
    db_session: AsyncSession, retention_user: User
) -> None:
    # Plant a credit transaction (financial ledger — must persist).
    db_session.add(
        Transaction(
            user_id=retention_user.id,
            type="credit",
            amount_micro_rub=1_000_000,
            balance_after_micro_rub=1_000_000,
            description="retention guard test",
            idempotency_key=f"ret-guard-{uuid.uuid4()}",
        )
    )
    await db_session.commit()

    count_before = (
        await db_session.execute(select(func.count()).select_from(Transaction))
    ).scalar()

    await run_data_retention(db_session)

    count_after = (
        await db_session.execute(select(func.count()).select_from(Transaction))
    ).scalar()
    assert count_after == count_before


# ── reconcile_partial_costs tests (tr-sgz) ──────────────────────────────────

BILLING_EMAIL = "test_billing_reconcile@example.com"


@pytest_asyncio.fixture
async def billing_user(db_session: AsyncSession):
    existing = (
        await db_session.execute(select(User).where(User.email_lower == BILLING_EMAIL.lower()))
    ).scalar_one_or_none()
    if existing is not None:
        uid = existing.id
        await db_session.execute(sa_delete(UsageLog).where(UsageLog.user_id == uid))
        await db_session.execute(sa_delete(User).where(User.id == uid))
        await db_session.commit()

    user = User(
        email=BILLING_EMAIL,
        display_name="Биллинг Тест",
        password_hash=None,
        role="user",
        status="active",
        balance_micro_rub=0,
        reserved_micro_rub=0,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    yield user

    db_session.expire_all()
    existing = (
        await db_session.execute(select(User).where(User.email_lower == BILLING_EMAIL.lower()))
    ).scalar_one_or_none()
    if existing is not None:
        uid = existing.id
        await db_session.execute(sa_delete(UsageLog).where(UsageLog.user_id == uid))
        await db_session.execute(sa_delete(User).where(User.id == uid))
        await db_session.commit()


def _async_return(value):
    async def _f(*args, **kwargs):
        return value
    return _f


async def _add_partial_usage(db, *, user_id, request_id, gen_id, cost=0, age_seconds=60):
    db.add(UsageLog(
        user_id=user_id,
        request_id=request_id,
        model_id="openai/gpt-4.1-mini",
        input_tokens=10, output_tokens=5,
        estimated_input_tokens=10, estimated_output_tokens=5,
        estimated_cost_micro_rub=0, actual_cost_micro_rub=100, reserved_micro_rub=100,
        openrouter_cost_micro_usd=cost, exchange_rate_micro_rub=90_000_000,
        status="partial", openrouter_request_id=gen_id,
        created_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    ))
    await db.commit()


async def test_reconcile_fills_partial_cost(db_session, billing_user, monkeypatch):
    rid = uuid.uuid4()
    await _add_partial_usage(db_session, user_id=billing_user.id, request_id=rid, gen_id="gen-r1")
    monkeypatch.setattr(cleanup_mod, "get_active_key", _async_return("sk-or-test"))
    monkeypatch.setattr(cleanup_mod, "fetch_generation_cost", _async_return(139000))

    updated = await reconcile_partial_costs(db_session)
    assert updated == 1
    db_session.expire_all()
    row = (await db_session.execute(select(UsageLog).where(UsageLog.request_id == rid))).scalar_one()
    assert row.openrouter_cost_micro_usd == 139000


async def test_reconcile_skips_when_not_ready(db_session, billing_user, monkeypatch):
    rid = uuid.uuid4()
    await _add_partial_usage(db_session, user_id=billing_user.id, request_id=rid, gen_id="gen-r2")
    monkeypatch.setattr(cleanup_mod, "get_active_key", _async_return("sk-or-test"))
    monkeypatch.setattr(cleanup_mod, "fetch_generation_cost", _async_return(None))

    updated = await reconcile_partial_costs(db_session)
    assert updated == 0
    db_session.expire_all()
    row = (await db_session.execute(select(UsageLog).where(UsageLog.request_id == rid))).scalar_one()
    assert row.openrouter_cost_micro_usd == 0


async def test_reconcile_ignores_too_new_and_too_old(db_session, billing_user, monkeypatch):
    too_new, too_old = uuid.uuid4(), uuid.uuid4()
    await _add_partial_usage(db_session, user_id=billing_user.id, request_id=too_new, gen_id="gen-new", age_seconds=2)
    await _add_partial_usage(db_session, user_id=billing_user.id, request_id=too_old, gen_id="gen-old", age_seconds=60*60*25)
    monkeypatch.setattr(cleanup_mod, "get_active_key", _async_return("sk-or-test"))
    monkeypatch.setattr(cleanup_mod, "fetch_generation_cost", _async_return(139000))

    updated = await reconcile_partial_costs(db_session)
    assert updated == 0


# ── reconcile completed cost_pending rows (tr-34mj) ─────────────────────────


async def _add_completed_usage(
    db, *, user_id, request_id, gen_id, cost=0, cost_pending=False, age_seconds=60
):
    db.add(UsageLog(
        user_id=user_id,
        request_id=request_id,
        model_id="anthropic/claude-opus-4.8",
        input_tokens=90_000, output_tokens=2,
        estimated_input_tokens=90_000, estimated_output_tokens=2,
        estimated_cost_micro_rub=0, actual_cost_micro_rub=13_000_000,
        reserved_micro_rub=26_000_000,
        openrouter_cost_micro_usd=cost, cost_pending=cost_pending,
        exchange_rate_micro_rub=90_000_000,
        status="completed", openrouter_request_id=gen_id,
        created_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    ))
    await db.commit()


async def test_reconcile_backfills_completed_cost_pending(
    db_session, billing_user, monkeypatch
):
    """tr-34mj: a COMPLETED row whose stream usage chunk carried no cost
    (cost_pending=true) is backfilled from /generation, and the pending flag is
    cleared on success."""
    rid = uuid.uuid4()
    await _add_completed_usage(
        db_session, user_id=billing_user.id, request_id=rid,
        gen_id="gen-c1", cost=0, cost_pending=True,
    )
    monkeypatch.setattr(cleanup_mod, "get_active_key", _async_return("sk-or-test"))
    monkeypatch.setattr(cleanup_mod, "fetch_generation_cost", _async_return(452_000))

    updated = await reconcile_partial_costs(db_session)
    assert updated == 1
    db_session.expire_all()
    row = (
        await db_session.execute(select(UsageLog).where(UsageLog.request_id == rid))
    ).scalar_one()
    assert row.openrouter_cost_micro_usd == 452_000
    assert row.cost_pending is False


async def test_reconcile_ignores_completed_when_cost_known(
    db_session, billing_user, monkeypatch
):
    """A completed row with a genuine $0 (free model, cost_pending=false) is NOT a
    backfill candidate — must not be re-queried or touched."""
    rid = uuid.uuid4()
    await _add_completed_usage(
        db_session, user_id=billing_user.id, request_id=rid,
        gen_id="gen-c2", cost=0, cost_pending=False,
    )
    monkeypatch.setattr(cleanup_mod, "get_active_key", _async_return("sk-or-test"))
    monkeypatch.setattr(cleanup_mod, "fetch_generation_cost", _async_return(999_000))

    updated = await reconcile_partial_costs(db_session)
    assert updated == 0
    db_session.expire_all()
    row = (
        await db_session.execute(select(UsageLog).where(UsageLog.request_id == rid))
    ).scalar_one()
    assert row.openrouter_cost_micro_usd == 0
    assert row.cost_pending is False
