import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import delete as sa_delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.exceptions import InsufficientFundsError
from app.models.reservation import Reservation
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services.billing_service import (
    RESERVATION_BUFFER_FACTOR,
    create_reservation,
    credit_balance,
    debit_balance,
    finalize_reservation,
    release_reservation,
)

TEST_EMAIL = "test_billing@example.com"


async def _purge_user(session: AsyncSession, email: str) -> None:
    existing = (
        await session.execute(select(User).where(User.email_lower == email.lower()))
    ).scalar_one_or_none()
    if existing is None:
        return
    user_id = existing.id
    await session.execute(sa_delete(UsageLog).where(UsageLog.user_id == user_id))
    await session.execute(sa_delete(Reservation).where(Reservation.user_id == user_id))
    await session.execute(sa_delete(Transaction).where(Transaction.user_id == user_id))
    await session.execute(sa_delete(User).where(User.id == user_id))
    await session.commit()


@pytest_asyncio.fixture
async def billing_user(db_session: AsyncSession):
    await _purge_user(db_session, TEST_EMAIL)

    user = User(
        email=TEST_EMAIL,
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
    await _purge_user(db_session, TEST_EMAIL)


async def test_credit_increases_balance(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    new_balance, was_applied = await credit_balance(
        db_session,
        user_id=user_id,
        amount_micro_rub=500_000_000,
        idempotency_key="key-credit-1",
        description="Пополнение баланса",
    )
    await db_session.commit()

    assert was_applied is True
    assert new_balance == 500_000_000

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub == 500_000_000


async def test_transaction_row_contents(
    db_session: AsyncSession, billing_user: User
) -> None:
    new_balance, _ = await credit_balance(
        db_session,
        user_id=billing_user.id,
        amount_micro_rub=500_000_000,
        idempotency_key="key-row-1",
        description="Top-up via YooKassa",
    )
    await db_session.commit()

    txn = (
        await db_session.execute(
            select(Transaction).where(Transaction.idempotency_key == "key-row-1")
        )
    ).scalar_one()

    assert txn.user_id == billing_user.id
    assert txn.type == "credit"
    assert txn.amount_micro_rub == 500_000_000
    assert txn.balance_after_micro_rub == new_balance
    assert txn.description == "Top-up via YooKassa"
    assert txn.payment_id is None
    assert txn.idempotency_key == "key-row-1"


async def test_balance_after_equals_new_balance(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    new_balance_1, _ = await credit_balance(
        db_session,
        user_id=user_id,
        amount_micro_rub=200_000_000,
        idempotency_key="key-after-1",
        description="First",
    )
    await db_session.commit()

    new_balance_2, _ = await credit_balance(
        db_session,
        user_id=user_id,
        amount_micro_rub=300_000_000,
        idempotency_key="key-after-2",
        description="Second",
    )
    await db_session.commit()

    txn1 = (
        await db_session.execute(
            select(Transaction).where(Transaction.idempotency_key == "key-after-1")
        )
    ).scalar_one()
    txn2 = (
        await db_session.execute(
            select(Transaction).where(Transaction.idempotency_key == "key-after-2")
        )
    ).scalar_one()

    assert txn1.balance_after_micro_rub == new_balance_1 == 200_000_000
    assert txn2.balance_after_micro_rub == new_balance_2 == 500_000_000

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub == 500_000_000


async def test_duplicate_idempotency_key_is_noop(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    new_balance_1, applied_1 = await credit_balance(
        db_session,
        user_id=user_id,
        amount_micro_rub=500_000_000,
        idempotency_key="key-dup-1",
        description="First call",
    )
    await db_session.commit()
    assert applied_1 is True
    assert new_balance_1 == 500_000_000

    new_balance_2, applied_2 = await credit_balance(
        db_session,
        user_id=user_id,
        amount_micro_rub=500_000_000,
        idempotency_key="key-dup-1",
        description="Duplicate call",
    )
    await db_session.commit()
    assert applied_2 is False
    assert new_balance_2 == 500_000_000

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub == 500_000_000

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.user_id == user_id)
        )
    ).scalar()
    assert count == 1


async def test_sum_invariant_after_multiple_credits(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    credits = [
        (100_000_000, "key-mc-1"),
        (250_000_000, "key-mc-2"),
        (75_000_000, "key-mc-3"),
        (1_000_000, "key-mc-4"),
    ]
    for amount, key in credits:
        await credit_balance(
            db_session,
            user_id=user_id,
            amount_micro_rub=amount,
            idempotency_key=key,
            description=f"Top-up {amount} μ₽",
        )
        await db_session.commit()

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    txn_sum = (
        await db_session.execute(
            select(func.coalesce(func.sum(Transaction.amount_micro_rub), 0)).where(
                Transaction.user_id == user_id
            )
        )
    ).scalar()

    expected = sum(amount for amount, _ in credits)
    assert user.balance_micro_rub == expected
    assert txn_sum == expected
    assert user.balance_micro_rub == txn_sum


async def test_concurrent_credits_produce_exact_total(
    engine: AsyncEngine, billing_user: User
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def credit_once(idx: int) -> tuple[int, bool]:
        async with factory() as session:
            result = await credit_balance(
                session,
                user_id=billing_user.id,
                amount_micro_rub=100_000_000,
                idempotency_key=f"key-conc-{idx}",
                description=f"Concurrent top-up #{idx}",
            )
            await session.commit()
            return result

    results = await asyncio.gather(*(credit_once(i) for i in range(5)))
    assert all(applied for _, applied in results)

    async with factory() as session:
        user = (
            await session.execute(select(User).where(User.id == billing_user.id))
        ).scalar_one()
        assert user.balance_micro_rub == 500_000_000

        count = (
            await session.execute(
                select(func.count())
                .select_from(Transaction)
                .where(Transaction.user_id == billing_user.id)
            )
        ).scalar()
        assert count == 5


# ── Debit tests ───────────────────────────────────────────────────────────────


async def _seed_balance(
    session: AsyncSession, user_id, amount_micro_rub: int, key_suffix: str
) -> None:
    await credit_balance(
        session,
        user_id=user_id,
        amount_micro_rub=amount_micro_rub,
        idempotency_key=f"seed-{key_suffix}",
        description="Test seed top-up",
    )
    await session.commit()


async def test_debit_decreases_balance(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    await _seed_balance(db_session, user_id, 500_000_000, "debit-norm")

    new_balance, actual, overdraft = await debit_balance(
        db_session,
        user_id=user_id,
        amount_micro_rub=100_000_000,
        idempotency_key="key-debit-1",
        description="API request charge",
    )
    await db_session.commit()

    assert overdraft is False
    assert actual == 100_000_000
    assert new_balance == 400_000_000

    txn = (
        await db_session.execute(
            select(Transaction).where(Transaction.idempotency_key == "key-debit-1")
        )
    ).scalar_one()
    assert txn.type == "debit"
    assert txn.amount_micro_rub == -100_000_000
    assert txn.balance_after_micro_rub == 400_000_000
    assert txn.user_id == user_id

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub == 400_000_000


async def test_debit_balance_after_equals_new_balance(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    await _seed_balance(db_session, user_id, 500_000_000, "debit-after")

    new_balance, _, _ = await debit_balance(
        db_session,
        user_id=user_id,
        amount_micro_rub=123_456_789,
        idempotency_key="key-debit-after-1",
        description="Charge",
    )
    await db_session.commit()

    txn = (
        await db_session.execute(
            select(Transaction).where(Transaction.idempotency_key == "key-debit-after-1")
        )
    ).scalar_one()
    txn_balance_after = txn.balance_after_micro_rub

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()

    assert txn_balance_after == new_balance
    assert txn_balance_after == user.balance_micro_rub


async def test_debit_overdraft_clamps_to_zero(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    await _seed_balance(db_session, user_id, 500_000_000, "debit-clamp")

    new_balance, actual, overdraft = await debit_balance(
        db_session,
        user_id=user_id,
        amount_micro_rub=600_000_000,
        idempotency_key="key-debit-clamp",
        description="Overdraft request",
    )
    await db_session.commit()

    assert overdraft is True
    assert actual == 500_000_000
    assert new_balance == 0

    txn = (
        await db_session.execute(
            select(Transaction).where(Transaction.idempotency_key == "key-debit-clamp")
        )
    ).scalar_one()
    assert txn.type == "debit"
    assert txn.amount_micro_rub == -500_000_000
    assert txn.balance_after_micro_rub == 0

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub == 0


async def test_debit_zero_balance_creates_no_transaction(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    # billing_user already has balance_micro_rub=0; no seeding.

    new_balance, actual, overdraft = await debit_balance(
        db_session,
        user_id=user_id,
        amount_micro_rub=10_000_000,
        idempotency_key="key-debit-zero",
        description="Charge against empty balance",
    )
    await db_session.commit()

    assert new_balance == 0
    assert actual == 0
    assert overdraft is True

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.user_id == user_id)
        )
    ).scalar()
    assert count == 0

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub == 0


async def test_debit_duplicate_idempotency_key_is_noop(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    await _seed_balance(db_session, user_id, 500_000_000, "debit-dup")

    new_balance_1, actual_1, overdraft_1 = await debit_balance(
        db_session,
        user_id=user_id,
        amount_micro_rub=100_000_000,
        idempotency_key="key-debit-dup-1",
        description="First debit",
    )
    await db_session.commit()
    assert actual_1 == 100_000_000
    assert new_balance_1 == 400_000_000
    assert overdraft_1 is False

    new_balance_2, actual_2, overdraft_2 = await debit_balance(
        db_session,
        user_id=user_id,
        amount_micro_rub=100_000_000,
        idempotency_key="key-debit-dup-1",  # SAME key
        description="Duplicate debit",
    )
    await db_session.commit()
    assert actual_2 == 0
    assert overdraft_2 is False
    assert new_balance_2 == 400_000_000

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub == 400_000_000

    debit_count = (
        await db_session.execute(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.user_id == user_id, Transaction.type == "debit")
        )
    ).scalar()
    assert debit_count == 1


async def test_sum_invariant_with_credits_and_debits(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id

    await credit_balance(
        db_session,
        user_id=user_id,
        amount_micro_rub=500_000_000,
        idempotency_key="mix-credit-1",
        description="Top-up",
    )
    await db_session.commit()

    await debit_balance(
        db_session,
        user_id=user_id,
        amount_micro_rub=80_000_000,
        idempotency_key="mix-debit-1",
        description="Charge 1",
    )
    await db_session.commit()

    await credit_balance(
        db_session,
        user_id=user_id,
        amount_micro_rub=120_000_000,
        idempotency_key="mix-credit-2",
        description="Top-up",
    )
    await db_session.commit()

    await debit_balance(
        db_session,
        user_id=user_id,
        amount_micro_rub=50_000_000,
        idempotency_key="mix-debit-2",
        description="Charge 2",
    )
    await db_session.commit()

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    txn_sum = (
        await db_session.execute(
            select(func.coalesce(func.sum(Transaction.amount_micro_rub), 0)).where(
                Transaction.user_id == user_id
            )
        )
    ).scalar()

    expected = 500_000_000 - 80_000_000 + 120_000_000 - 50_000_000
    assert user.balance_micro_rub == expected
    assert txn_sum == expected


async def test_db_check_constraint_rejects_negative_balance(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text("UPDATE users SET balance_micro_rub = -1 WHERE id = :uid"),
            {"uid": user_id},
        )
        await db_session.commit()

    await db_session.rollback()

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub == 0


# ── Reservation tests ─────────────────────────────────────────────────────────


async def test_create_reservation_locks_funds(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "res-create-1")

    reservation = await create_reservation(
        db_session,
        user_id=user_id,
        estimated_cost_micro_rub=100_000_000,
        model_id="openai/gpt-4o",
    )
    reservation_id = reservation.id
    reserved_amount = reservation.reserved_micro_rub
    status = reservation.status
    await db_session.commit()

    assert status == "active"
    assert reserved_amount == 200_000_000  # 100M × 2× buffer

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub == 1_000_000_000
    assert user.reserved_micro_rub == 200_000_000

    res = (
        await db_session.execute(select(Reservation).where(Reservation.id == reservation_id))
    ).scalar_one()
    assert res.status == "active"
    assert res.reserved_micro_rub == 200_000_000
    assert res.model_id == "openai/gpt-4o"


async def test_create_reservation_available_subtracts_reserved(
    db_session: AsyncSession, billing_user: User
) -> None:
    """available = balance - reserved, not just balance."""
    user_id = billing_user.id
    await _seed_balance(db_session, user_id, 300_000_000, "res-avail-seed")

    # Pre-existing reservation eats 200M (100M est × 2)
    await create_reservation(
        db_session,
        user_id=user_id,
        estimated_cost_micro_rub=100_000_000,
        model_id="openai/gpt-4o",
    )
    await db_session.commit()

    # Now available = 300M - 200M = 100M.
    # A 60M estimate would normally desire 120M reservation, but we'll clamp to 100M.
    reservation = await create_reservation(
        db_session,
        user_id=user_id,
        estimated_cost_micro_rub=60_000_000,
        model_id="openai/gpt-4o",
    )
    reserved_amount = reservation.reserved_micro_rub
    await db_session.commit()

    # min(available=100M, desired=120M) = 100M
    assert reserved_amount == 100_000_000

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.reserved_micro_rub == 300_000_000  # 200M + 100M


async def test_create_reservation_insufficient_funds(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    await _seed_balance(db_session, user_id, 50_000_000, "res-insuf")

    with pytest.raises(InsufficientFundsError):
        await create_reservation(
            db_session,
            user_id=user_id,
            estimated_cost_micro_rub=100_000_000,
            model_id="openai/gpt-4o",
        )
    await db_session.rollback()

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.reserved_micro_rub == 0
    assert user.balance_micro_rub == 50_000_000


async def test_two_sequential_reservations_accumulate(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "res-seq")

    r1 = await create_reservation(
        db_session,
        user_id=user_id,
        estimated_cost_micro_rub=100_000_000,
        model_id="openai/gpt-4o",
    )
    r1_amount = r1.reserved_micro_rub
    await db_session.commit()

    r2 = await create_reservation(
        db_session,
        user_id=user_id,
        estimated_cost_micro_rub=50_000_000,
        model_id="openai/gpt-4o",
    )
    r2_amount = r2.reserved_micro_rub
    await db_session.commit()

    assert r1_amount == 200_000_000
    assert r2_amount == 100_000_000

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.reserved_micro_rub == 300_000_000


async def test_db_check_rejects_reserved_exceeding_balance(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    # billing_user has balance=0, reserved=0

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "UPDATE users SET reserved_micro_rub = 1 WHERE id = :uid"
            ),
            {"uid": user_id},
        )
        await db_session.commit()

    await db_session.rollback()


async def test_finalize_reservation_normal(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "fin-norm")

    reservation = await create_reservation(
        db_session,
        user_id=user_id,
        estimated_cost_micro_rub=100_000_000,
        model_id="openai/gpt-4o",
    )
    reservation_id = reservation.id
    request_id = reservation.request_id
    reserved_amount = reservation.reserved_micro_rub
    await db_session.commit()

    actual_debited, overdraft = await finalize_reservation(
        db_session,
        reservation_id=reservation_id,
        actual_cost_micro_rub=80_000_000,
        prompt_tokens=300,
        completion_tokens=200,
        model_id="openai/gpt-4o",
    )
    await db_session.commit()

    assert actual_debited == 80_000_000
    assert overdraft is False

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub == 1_000_000_000 - 80_000_000
    assert user.reserved_micro_rub == 0  # released the 200M reservation

    res = (
        await db_session.execute(select(Reservation).where(Reservation.id == reservation_id))
    ).scalar_one()
    assert res.status == "finalized"
    assert res.actual_micro_rub == 80_000_000
    assert res.refunded_micro_rub == reserved_amount - 80_000_000

    txn = (
        await db_session.execute(
            select(Transaction).where(
                Transaction.idempotency_key == str(request_id)
            )
        )
    ).scalar_one()
    assert txn.type == "debit"
    assert txn.amount_micro_rub == -80_000_000

    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == request_id)
        )
    ).scalar_one()
    assert usage.input_tokens == 300
    assert usage.output_tokens == 200
    assert usage.actual_cost_micro_rub == 80_000_000
    assert usage.reserved_micro_rub == reserved_amount
    assert usage.status == "completed"
    assert usage.overdraft_absorbed_micro_rub == 0
    assert usage.cost_pending is False  # cost known by default


async def test_finalize_reservation_marks_cost_pending(
    db_session: AsyncSession, billing_user: User
) -> None:
    """tr-34mj: when the stream usage chunk carried no cost, finalize records
    cost_pending=true so the backfill cron can later fill the real cost."""
    user_id = billing_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "fin-pending")

    reservation = await create_reservation(
        db_session,
        user_id=user_id,
        estimated_cost_micro_rub=10_000_000,
        model_id="anthropic/claude-opus-4.8",
    )
    request_id = reservation.request_id
    await db_session.commit()

    await finalize_reservation(
        db_session,
        reservation_id=reservation.id,
        actual_cost_micro_rub=5_000_000,
        prompt_tokens=90_000,
        completion_tokens=2,
        model_id="anthropic/claude-opus-4.8",
        openrouter_cost_micro_usd=0,
        cost_pending=True,
    )
    await db_session.commit()

    db_session.expire_all()
    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == request_id)
        )
    ).scalar_one()
    assert usage.cost_pending is True
    assert usage.openrouter_cost_micro_usd == 0


async def test_finalize_reservation_overdraft_clamps(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    await _seed_balance(db_session, user_id, 500_000_000, "fin-over")

    reservation = await create_reservation(
        db_session,
        user_id=user_id,
        estimated_cost_micro_rub=100_000_000,
        model_id="openai/gpt-4o",
    )
    reservation_id = reservation.id
    request_id = reservation.request_id
    await db_session.commit()

    actual_debited, overdraft = await finalize_reservation(
        db_session,
        reservation_id=reservation_id,
        actual_cost_micro_rub=600_000_000,
        prompt_tokens=300,
        completion_tokens=200,
        model_id="openai/gpt-4o",
    )
    await db_session.commit()

    assert actual_debited == 500_000_000
    assert overdraft is True

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub == 0
    assert user.reserved_micro_rub == 0

    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == request_id)
        )
    ).scalar_one()
    assert usage.actual_cost_micro_rub == 500_000_000
    assert usage.overdraft_absorbed_micro_rub == 100_000_000


async def test_finalize_already_finalized_raises_400(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "fin-double")

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

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        await finalize_reservation(
            db_session,
            reservation_id=reservation_id,
            actual_cost_micro_rub=80_000_000,
            prompt_tokens=10,
            completion_tokens=10,
            model_id="openai/gpt-4o",
        )
    assert exc_info.value.status_code == 400
    await db_session.rollback()


async def test_release_reservation_normal(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "rel-norm")

    reservation = await create_reservation(
        db_session,
        user_id=user_id,
        estimated_cost_micro_rub=100_000_000,
        model_id="openai/gpt-4o",
    )
    reservation_id = reservation.id
    request_id = reservation.request_id
    await db_session.commit()

    await release_reservation(db_session, reservation_id=reservation_id)
    await db_session.commit()

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub == 1_000_000_000  # unchanged
    assert user.reserved_micro_rub == 0

    res = (
        await db_session.execute(select(Reservation).where(Reservation.id == reservation_id))
    ).scalar_one()
    assert res.status == "released"

    txn_count = (
        await db_session.execute(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.user_id == user_id, Transaction.type == "debit")
        )
    ).scalar()
    assert txn_count == 0

    usage_count = (
        await db_session.execute(
            select(func.count())
            .select_from(UsageLog)
            .where(UsageLog.request_id == request_id)
        )
    ).scalar()
    assert usage_count == 0


async def test_release_reservation_with_failure_reason_creates_usage_log(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "rel-fail")

    reservation = await create_reservation(
        db_session,
        user_id=user_id,
        estimated_cost_micro_rub=100_000_000,
        model_id="openai/gpt-4o",
    )
    reservation_id = reservation.id
    request_id = reservation.request_id
    reserved_amount = reservation.reserved_micro_rub
    await db_session.commit()

    await release_reservation(
        db_session,
        reservation_id=reservation_id,
        failure_reason="OpenRouter timeout after 30s",
    )
    await db_session.commit()

    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == request_id)
        )
    ).scalar_one()
    assert usage.status == "failed"
    assert usage.error_message == "OpenRouter timeout after 30s"
    assert usage.actual_cost_micro_rub == 0
    assert usage.reserved_micro_rub == reserved_amount
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub == 1_000_000_000  # not debited
    assert user.reserved_micro_rub == 0


async def test_release_already_released_raises_400(
    db_session: AsyncSession, billing_user: User
) -> None:
    user_id = billing_user.id
    await _seed_balance(db_session, user_id, 1_000_000_000, "rel-double")

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

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        await release_reservation(db_session, reservation_id=reservation_id)
    assert exc_info.value.status_code == 400
    await db_session.rollback()
