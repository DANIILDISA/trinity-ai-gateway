"""Concurrent balance-safety tests.

Each test fires concurrent operations via asyncio.gather and verifies the
two non-negotiable invariants after all coroutines settle:

  1. user.balance_micro_rub >= 0  (DB CHECK enforces; assert anyway)
  2. user.balance_micro_rub == SUM(transactions.amount_micro_rub WHERE user_id=…)

Plus the per-scenario assertions called out by Task 2.5.
"""
import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.exceptions import InsufficientFundsError
from app.models.reservation import Reservation
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services.billing_service import (
    create_reservation,
    credit_balance,
    debit_balance,
    finalize_reservation,
    release_reservation,
)

TEST_EMAIL = "test_concurrent@example.com"


async def _purge(session: AsyncSession, email: str) -> None:
    existing = (
        await session.execute(select(User).where(User.email_lower == email.lower()))
    ).scalar_one_or_none()
    if existing is None:
        return
    uid = existing.id
    await session.execute(sa_delete(UsageLog).where(UsageLog.user_id == uid))
    await session.execute(sa_delete(Reservation).where(Reservation.user_id == uid))
    await session.execute(sa_delete(Transaction).where(Transaction.user_id == uid))
    await session.execute(sa_delete(User).where(User.id == uid))
    await session.commit()


@pytest_asyncio.fixture
async def concurrent_user(db_session: AsyncSession):
    await _purge(db_session, TEST_EMAIL)

    user = User(
        email=TEST_EMAIL,
        display_name="Конкурентный Тест",
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


async def _seed_balance(
    factory: async_sessionmaker[AsyncSession], user_id, amount: int
) -> None:
    async with factory() as s:
        await credit_balance(
            s,
            user_id=user_id,
            amount_micro_rub=amount,
            idempotency_key=f"seed-{uuid.uuid4()}",
            description="Seed balance",
        )
        await s.commit()


async def _assert_balance_equals_transaction_sum(
    factory: async_sessionmaker[AsyncSession], user_id
) -> int:
    """Return the final balance after verifying the core invariant."""
    async with factory() as s:
        balance = (
            await s.execute(
                select(User.balance_micro_rub).where(User.id == user_id)
            )
        ).scalar_one()
        txn_sum = (
            await s.execute(
                select(func.coalesce(func.sum(Transaction.amount_micro_rub), 0)).where(
                    Transaction.user_id == user_id
                )
            )
        ).scalar()
    assert balance >= 0, f"Negative balance after concurrent ops: {balance}"
    assert balance == txn_sum, (
        f"Ledger invariant broken: balance={balance} vs SUM(txn)={txn_sum}"
    )
    return balance


# ── Test 1: Double reservation race ──────────────────────────────────────────


async def test_double_reservation_race(
    engine: AsyncEngine, concurrent_user: User
) -> None:
    user_id = concurrent_user.id
    factory = async_sessionmaker(engine, expire_on_commit=False)

    await _seed_balance(factory, user_id, 100_000_000)

    async def attempt() -> object:
        async with factory() as s:
            res = await create_reservation(
                s,
                user_id=user_id,
                estimated_cost_micro_rub=80_000_000,
                model_id="openai/gpt-4o",
            )
            await s.commit()
            return res.reserved_micro_rub

    results = await asyncio.gather(
        attempt(), attempt(), return_exceptions=True
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]

    # Exactly one succeeds, one fails with InsufficientFundsError.
    assert len(successes) == 1, f"Expected 1 success, got {len(successes)}: {results}"
    assert len(failures) == 1
    assert isinstance(failures[0], InsufficientFundsError)

    # Single succeeding reservation: estimated=80M × 2 buffer = 160M desired,
    # clamped to available (100M). So reserved ends at 100M.
    assert successes[0] == 100_000_000

    async with factory() as s:
        user = (
            await s.execute(select(User).where(User.id == user_id))
        ).scalar_one()
        assert user.balance_micro_rub == 100_000_000  # unchanged
        assert user.reserved_micro_rub == 100_000_000
        assert user.balance_micro_rub >= user.reserved_micro_rub  # CHECK invariant

        res_count = (
            await s.execute(
                select(func.count())
                .select_from(Reservation)
                .where(Reservation.user_id == user_id)
            )
        ).scalar()
        assert res_count == 1  # only the winner inserted a row

    await _assert_balance_equals_transaction_sum(factory, user_id)


# ── Test 2: Credit + debit race ──────────────────────────────────────────────


async def test_credit_debit_race(
    engine: AsyncEngine, concurrent_user: User
) -> None:
    user_id = concurrent_user.id
    factory = async_sessionmaker(engine, expire_on_commit=False)

    credit_key = f"credit-{uuid.uuid4()}"
    debit_key = f"debit-{uuid.uuid4()}"

    async def do_credit():
        async with factory() as s:
            result = await credit_balance(
                s,
                user_id=user_id,
                amount_micro_rub=100_000_000,
                idempotency_key=credit_key,
                description="Concurrent credit",
            )
            await s.commit()
            return result

    async def do_debit():
        async with factory() as s:
            result = await debit_balance(
                s,
                user_id=user_id,
                amount_micro_rub=50_000_000,
                idempotency_key=debit_key,
                description="Concurrent debit",
            )
            await s.commit()
            return result

    results = await asyncio.gather(
        do_credit(), do_debit(), return_exceptions=True
    )
    # Neither operation should raise — credit always succeeds; debit no-ops on 0.
    assert not any(isinstance(r, BaseException) for r in results), results

    final_balance = await _assert_balance_equals_transaction_sum(factory, user_id)

    # Two legitimate orderings under SELECT FOR UPDATE serialization:
    #   A) credit committed first → debit sees 100M → debits 50M → balance = 50M
    #   B) debit committed first → balance was 0 → debit no-ops → credit adds → 100M
    assert final_balance in (50_000_000, 100_000_000), (
        f"Unexpected balance {final_balance}; valid outcomes are 50M or 100M"
    )

    async with factory() as s:
        # Balance never went negative at any intermediate point — proven by the
        # DB CHECK constraint not having fired. Re-assert here.
        balance = (
            await s.execute(
                select(User.balance_micro_rub).where(User.id == user_id)
            )
        ).scalar_one()
        assert balance >= 0


# ── Test 3: Multi-reservation race ───────────────────────────────────────────


async def test_multi_reservation_race(
    engine: AsyncEngine, concurrent_user: User
) -> None:
    user_id = concurrent_user.id
    factory = async_sessionmaker(engine, expire_on_commit=False)

    await _seed_balance(factory, user_id, 1_000_000_000)

    async def attempt():
        async with factory() as s:
            res = await create_reservation(
                s,
                user_id=user_id,
                estimated_cost_micro_rub=300_000_000,
                model_id="openai/gpt-4o",
            )
            await s.commit()
            return res.reserved_micro_rub

    results = await asyncio.gather(
        *(attempt() for _ in range(5)), return_exceptions=True
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]

    assert len(successes) + len(failures) == 5
    # All failures must be InsufficientFundsError, never something else.
    for f in failures:
        assert isinstance(f, InsufficientFundsError), f
    # Per task: at most 3 succeed. With 2× buffer the realistic ceiling is 2,
    # but the loose bound is what we contract on.
    assert 1 <= len(successes) <= 3, f"successes={len(successes)}, results={results}"

    async with factory() as s:
        user = (
            await s.execute(select(User).where(User.id == user_id))
        ).scalar_one()
        assert user.balance_micro_rub == 1_000_000_000  # unchanged
        assert user.reserved_micro_rub <= 1_000_000_000
        assert user.balance_micro_rub >= user.reserved_micro_rub  # CHECK invariant

        # Sum of inserted reservation rows equals user.reserved_micro_rub
        reserved_sum = (
            await s.execute(
                select(func.coalesce(func.sum(Reservation.reserved_micro_rub), 0))
                .where(Reservation.user_id == user_id)
            )
        ).scalar()
        assert reserved_sum == user.reserved_micro_rub == sum(successes)

    await _assert_balance_equals_transaction_sum(factory, user_id)


# ── Test 4: Finalize + release race ──────────────────────────────────────────


async def test_finalize_release_race(
    engine: AsyncEngine, concurrent_user: User
) -> None:
    user_id = concurrent_user.id
    factory = async_sessionmaker(engine, expire_on_commit=False)

    await _seed_balance(factory, user_id, 500_000_000)

    # estimated=100M × 2 buffer = 200M reserved
    async with factory() as s:
        reservation = await create_reservation(
            s,
            user_id=user_id,
            estimated_cost_micro_rub=100_000_000,
            model_id="openai/gpt-4o",
        )
        reservation_id = reservation.id
        reserved_amount = reservation.reserved_micro_rub
        await s.commit()
    assert reserved_amount == 200_000_000

    async def do_finalize():
        async with factory() as s:
            result = await finalize_reservation(
                s,
                reservation_id=reservation_id,
                actual_cost_micro_rub=150_000_000,
                prompt_tokens=100,
                completion_tokens=100,
                model_id="openai/gpt-4o",
            )
            await s.commit()
            return ("finalized", result)

    async def do_release():
        async with factory() as s:
            await release_reservation(s, reservation_id=reservation_id)
            await s.commit()
            return ("released", None)

    results = await asyncio.gather(
        do_finalize(), do_release(), return_exceptions=True
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]

    # Exactly one succeeds; the loser raises 400 because the reservation is no
    # longer 'active' by the time it acquires the row lock.
    assert len(successes) == 1, f"Expected 1 success, got {results}"
    assert len(failures) == 1
    from fastapi import HTTPException
    assert isinstance(failures[0], HTTPException)
    assert failures[0].status_code == 400

    async with factory() as s:
        res = (
            await s.execute(select(Reservation).where(Reservation.id == reservation_id))
        ).scalar_one()
        assert res.status in ("finalized", "released")
        assert res.status != "active"

        user = (
            await s.execute(select(User).where(User.id == user_id))
        ).scalar_one()
        assert user.reserved_micro_rub == 0

    await _assert_balance_equals_transaction_sum(factory, user_id)
