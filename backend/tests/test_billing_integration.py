"""End-to-end billing integration tests (BILLING_LOGIC.md §1.3 invariants).

Every scenario calls `assert_financial_invariants(...)` at the end to verify
the six critical invariants. The checks use raw SQL so they read the actual
DB state, not the SQLAlchemy session cache.
"""
import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import delete as sa_delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from redis.asyncio import Redis

from app.core.exceptions import InsufficientFundsError, OpenRouterServerError
from app.models.alert import Alert
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.reservation import Reservation
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services.billing_service import credit_balance, process_chat_request
from app.services.billing_types import SSEEvent
from app.services.openrouter_proxy import StreamChunk

MODEL_ID = "openai/gpt-4.1-mini"


# ── Invariant checker ───────────────────────────────────────────────────────


async def assert_financial_invariants(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Verify all 6 financial invariants from BILLING_LOGIC.md §1.3.

    Raw SQL is intentional: it reads the persisted state rather than any
    ORM identity-map cache the test session may be holding.
    """
    # Force fresh reads — earlier-stage operations in the same test may have
    # populated the session cache with values that no longer reflect the DB.
    db.expire_all()

    # Invariant 1: balance == SUM(transactions.amount_micro_rub) for this user.
    balance = (
        await db.execute(
            text("SELECT balance_micro_rub FROM users WHERE id = :uid"),
            {"uid": user_id},
        )
    ).scalar_one()
    txn_sum = (
        await db.execute(
            text(
                "SELECT COALESCE(SUM(amount_micro_rub), 0) "
                "FROM transactions WHERE user_id = :uid"
            ),
            {"uid": user_id},
        )
    ).scalar_one()
    assert balance == txn_sum, (
        f"Inv 1 broken: balance={balance} ≠ SUM(transactions)={txn_sum}"
    )

    # Invariant 2: balance >= 0 (DB CHECK enforces — assert anyway).
    assert balance >= 0, f"Inv 2 broken: balance={balance} is negative"

    # Invariant 3: balance >= reserved (DB CHECK enforces — assert anyway).
    reserved = (
        await db.execute(
            text("SELECT reserved_micro_rub FROM users WHERE id = :uid"),
            {"uid": user_id},
        )
    ).scalar_one()
    assert balance >= reserved, (
        f"Inv 3 broken: balance={balance} < reserved={reserved}"
    )

    # Invariant 4: no duplicate idempotency_keys (global check across all users).
    dups = (
        await db.execute(
            text(
                "SELECT idempotency_key, COUNT(*) AS n "
                "FROM transactions "
                "WHERE idempotency_key IS NOT NULL "
                "GROUP BY idempotency_key HAVING COUNT(*) > 1"
            )
        )
    ).all()
    assert dups == [], f"Inv 4 broken: duplicate idempotency_keys: {dups}"

    # Invariant 5: every debit transaction has a usage_log with the same request_id.
    orphan_debits = (
        await db.execute(
            text(
                "SELECT t.id, t.request_id FROM transactions t "
                "LEFT JOIN usage_logs u ON u.request_id = t.request_id "
                "WHERE t.user_id = :uid AND t.type = 'debit' AND u.id IS NULL"
            ),
            {"uid": user_id},
        )
    ).all()
    assert orphan_debits == [], (
        f"Inv 5 broken: debit transactions without usage_log: {orphan_debits}"
    )

    # Invariant 6: every completed usage_log has a debit transaction.
    orphan_logs = (
        await db.execute(
            text(
                "SELECT u.id, u.request_id FROM usage_logs u "
                "LEFT JOIN transactions t "
                "ON t.request_id = u.request_id AND t.type = 'debit' "
                "WHERE u.user_id = :uid AND u.status = 'completed' "
                "AND t.id IS NULL"
            ),
            {"uid": user_id},
        )
    ).all()
    assert orphan_logs == [], (
        f"Inv 6 broken: completed usage_logs without debit: {orphan_logs}"
    )


# ── Fixtures and helpers ────────────────────────────────────────────────────


async def _purge_user_by_id(session: AsyncSession, uid: uuid.UUID) -> None:
    await session.execute(sa_delete(Message).where(
        Message.conversation_id.in_(
            select(Conversation.id).where(Conversation.user_id == uid)
        )
    ))
    await session.execute(sa_delete(Conversation).where(Conversation.user_id == uid))
    await session.execute(sa_delete(UsageLog).where(UsageLog.user_id == uid))
    await session.execute(sa_delete(Reservation).where(Reservation.user_id == uid))
    await session.execute(sa_delete(Transaction).where(Transaction.user_id == uid))
    await session.execute(sa_delete(User).where(User.id == uid))
    await session.commit()


@pytest_asyncio.fixture(autouse=True)
async def _isolate_alerts(db_session: AsyncSession):
    await db_session.execute(
        sa_delete(Alert).where(Alert.source.in_(["billing", "openrouter", "rate_limiter"]))
    )
    await db_session.commit()
    yield
    await db_session.execute(
        sa_delete(Alert).where(Alert.source.in_(["billing", "openrouter", "rate_limiter"]))
    )
    await db_session.commit()


@pytest_asyncio.fixture(autouse=True)
async def _clear_rate_limit(redis_client: Redis):
    keys = await redis_client.keys("ratelimit:*")
    if keys:
        await redis_client.delete(*keys)
    yield
    keys = await redis_client.keys("ratelimit:*")
    if keys:
        await redis_client.delete(*keys)


@pytest_asyncio.fixture(autouse=True)
async def _use_app_factory(app_session_factory):
    """Completed finalize runs on a FRESH get_session_factory() session (tr-sgz),
    so the global app factory must be initialized for these direct-call tests."""
    yield


async def _make_user(db: AsyncSession, email: str, balance_micro_rub: int) -> User:
    """Create a user with the requested starting balance backed by a credit txn.

    Inv 1 requires balance == SUM(transactions), so we cannot set balance via a
    raw INSERT and instead route the initial top-up through credit_balance.
    """
    existing = (
        await db.execute(select(User).where(User.email_lower == email.lower()))
    ).scalar_one_or_none()
    if existing is not None:
        await _purge_user_by_id(db, existing.id)

    user = User(
        email=email,
        display_name="Интеграция Тест",
        password_hash=None,
        role="user",
        status="active",
        balance_micro_rub=0,
        reserved_micro_rub=0,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    if balance_micro_rub > 0:
        await credit_balance(
            db,
            user_id=user.id,
            amount_micro_rub=balance_micro_rub,
            idempotency_key=f"seed-{user.id}",
            description="Initial seed balance",
        )
        await db.commit()
        await db.refresh(user)

    return user


async def _make_conversation(db: AsyncSession, user_id: uuid.UUID) -> Conversation:
    conv = Conversation(user_id=user_id, model_id=MODEL_ID, title="Integ")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


class _FakeProxy:
    def __init__(
        self,
        chunks: list[StreamChunk] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.chunks = chunks or []
        self.raises = raises

    def proxy_to_openrouter_with_retry(
        self,
        messages: list[dict],
        model_id: str,
        max_retries: int = 3,
        plugins: list[dict] | None = None,
        reasoning_budget: int | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        async def gen():
            for chunk in self.chunks:
                yield chunk
            if self.raises is not None:
                raise self.raises

        return gen()


def _c(text: str) -> StreamChunk:
    return StreamChunk(type="content", delta=text)


def _u(inp: int, out: int) -> StreamChunk:
    return StreamChunk(type="usage", input_tokens=inp, output_tokens=out)


def _d() -> StreamChunk:
    return StreamChunk(type="done")


def _ok_proxy(input_tokens: int = 10, output_tokens: int = 5) -> _FakeProxy:
    return _FakeProxy(chunks=[
        _c("ответ "),
        _c("готов."),
        _u(input_tokens, output_tokens),
        _d(),
    ])


async def _drain(gen: AsyncGenerator[SSEEvent, None]) -> list[SSEEvent]:
    return [evt async for evt in gen]


# ── Scenario 1 ──────────────────────────────────────────────────────────────


async def test_scenario_1_happy_path_three_messages(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    user = await _make_user(db_session, "integ_s1@example.com", 1_000_000_000)
    conv = await _make_conversation(db_session, user.id)
    user_id = user.id
    conv_id = conv.id
    try:
        starting_balance = user.balance_micro_rub

        for i in range(3):
            await _drain(process_chat_request(
                user_id=user_id,
                conversation_id=conv_id,
                message_content=f"Сообщение {i}",
                model_id=MODEL_ID,
                db=db_session,
                redis=redis_client,
                proxy=_ok_proxy(),
            ))

        db_session.expire_all()
        balance = (
            await db_session.execute(
                text("SELECT balance_micro_rub FROM users WHERE id = :uid"),
                {"uid": user_id},
            )
        ).scalar_one()
        assert balance < starting_balance, "balance must decrease after 3 requests"

        debit_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM transactions "
                    "WHERE user_id = :uid AND type = 'debit'"
                ),
                {"uid": user_id},
            )
        ).scalar_one()
        assert debit_count == 3, f"expected 3 debit txns, got {debit_count}"

        completed_logs = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM usage_logs "
                    "WHERE user_id = :uid AND status = 'completed'"
                ),
                {"uid": user_id},
            )
        ).scalar_one()
        assert completed_logs == 3, f"expected 3 completed logs, got {completed_logs}"

        await assert_financial_invariants(db_session, user_id)
    finally:
        await _purge_user_by_id(db_session, user_id)


# ── Scenario 2 ──────────────────────────────────────────────────────────────


async def test_scenario_2_failed_request_balance_unchanged(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    user = await _make_user(db_session, "integ_s2@example.com", 500_000_000)
    conv = await _make_conversation(db_session, user.id)
    user_id = user.id
    conv_id = conv.id
    try:
        starting_balance = 500_000_000

        proxy = _FakeProxy(raises=OpenRouterServerError())
        await _drain(process_chat_request(
            user_id=user_id,
            conversation_id=conv_id,
            message_content="Привет",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=proxy,
        ))

        db_session.expire_all()
        balance = (
            await db_session.execute(
                text("SELECT balance_micro_rub FROM users WHERE id = :uid"),
                {"uid": user_id},
            )
        ).scalar_one()
        assert balance == starting_balance, (
            f"balance changed unexpectedly: {balance} vs {starting_balance}"
        )

        debit_count = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM transactions "
                    "WHERE user_id = :uid AND type = 'debit'"
                ),
                {"uid": user_id},
            )
        ).scalar_one()
        assert debit_count == 0

        failed_logs = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM usage_logs "
                    "WHERE user_id = :uid AND status = 'failed'"
                ),
                {"uid": user_id},
            )
        ).scalar_one()
        assert failed_logs == 1

        await assert_financial_invariants(db_session, user_id)
    finally:
        await _purge_user_by_id(db_session, user_id)


# ── Scenario 3 ──────────────────────────────────────────────────────────────


async def test_scenario_3_concurrent_requests_one_succeeds(
    db_session: AsyncSession, redis_client: Redis, engine: AsyncEngine
) -> None:
    """Two concurrent process_chat_request calls; SELECT FOR UPDATE must serialize.

    Sets balance to exactly the estimated_cost of a tiny request. The first
    caller's create_reservation clamps to `min(available, 2× estimated)` =
    estimated (partial buffer), bringing reserved up to balance. The second
    caller, on acquiring the lock, sees available = 0 < estimated and raises
    InsufficientFundsError before creating its own reservation.
    """
    from app.models.model_pricing import ModelPricing
    from app.services.token_estimator import estimate_request_cost_with_history

    msg = "Привет"
    # Size the balance with the SAME estimator the pipeline uses
    # (estimate_request_cost_with_history), or balance won't match the pipeline's
    # estimate and even the first caller would fail. With no conversation history
    # this matches what each concurrent request computes at estimation time.
    pricing = (
        await db_session.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == MODEL_ID,
                ModelPricing.is_current.is_(True),
            )
        )
    ).scalar_one()
    est = await estimate_request_cost_with_history(
        db_session, redis_client, None, msg, MODEL_ID, pricing.max_output_tokens
    )
    user = await _make_user(
        db_session, "integ_s3@example.com",
        balance_micro_rub=est.estimated_cost_micro_rub,
    )
    conv = await _make_conversation(db_session, user.id)
    user_id = user.id
    conv_id = conv.id

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def one_request() -> str:
        async with factory() as s:
            try:
                await _drain(process_chat_request(
                    user_id=user_id,
                    conversation_id=conv_id,
                    message_content=msg,
                    model_id=MODEL_ID,
                    db=s,
                    redis=redis_client,
                    proxy=_ok_proxy(input_tokens=2, output_tokens=2),
                ))
                return "ok"
            except InsufficientFundsError:
                return "insufficient"

    try:
        results = await asyncio.gather(
            one_request(), one_request(), return_exceptions=True
        )

        # No raised exceptions should bubble up (one_request catches InsufficientFundsError).
        for r in results:
            assert not isinstance(r, BaseException), f"unexpected exception: {r!r}"

        successes = [r for r in results if r == "ok"]
        failures = [r for r in results if r == "insufficient"]
        assert len(successes) == 1, f"expected 1 success, got {results}"
        assert len(failures) == 1, f"expected 1 failure, got {results}"

        # Open a fresh session for verification — db_session was not involved in
        # the concurrent run and may hold stale cached state.
        async with factory() as verify:
            await assert_financial_invariants(verify, user_id)

            debit_count = (
                await verify.execute(
                    text(
                        "SELECT COUNT(*) FROM transactions "
                        "WHERE user_id = :uid AND type = 'debit'"
                    ),
                    {"uid": user_id},
                )
            ).scalar_one()
            assert debit_count == 1

            completed_logs = (
                await verify.execute(
                    text(
                        "SELECT COUNT(*) FROM usage_logs "
                        "WHERE user_id = :uid AND status = 'completed'"
                    ),
                    {"uid": user_id},
                )
            ).scalar_one()
            assert completed_logs == 1
    finally:
        async with factory() as s:
            await _purge_user_by_id(s, user_id)


# ── Scenario 4 ──────────────────────────────────────────────────────────────


async def test_scenario_4_refund_after_charge(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    user = await _make_user(db_session, "integ_s4@example.com", 1_000_000_000)
    conv = await _make_conversation(db_session, user.id)
    user_id = user.id
    conv_id = conv.id
    try:
        # 1) Run one successful request.
        await _drain(process_chat_request(
            user_id=user_id,
            conversation_id=conv_id,
            message_content="Привет",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=_ok_proxy(),
        ))

        db_session.expire_all()
        balance_after = (
            await db_session.execute(
                text("SELECT balance_micro_rub FROM users WHERE id = :uid"),
                {"uid": user_id},
            )
        ).scalar_one()
        debit = (
            await db_session.execute(
                text(
                    "SELECT amount_micro_rub, request_id FROM transactions "
                    "WHERE user_id = :uid AND type = 'debit' LIMIT 1"
                ),
                {"uid": user_id},
            )
        ).one()
        actual_cost = -debit.amount_micro_rub  # debit amount is negative
        request_id = debit.request_id
        assert actual_cost > 0

        # 2) Manual refund: credit transaction restoring actual_cost.
        await credit_balance(
            db_session,
            user_id=user_id,
            amount_micro_rub=actual_cost,
            idempotency_key=f"refund-{request_id}",
            description=f"Refund for request {request_id}",
        )
        await db_session.commit()

        db_session.expire_all()
        new_balance = (
            await db_session.execute(
                text("SELECT balance_micro_rub FROM users WHERE id = :uid"),
                {"uid": user_id},
            )
        ).scalar_one()
        assert new_balance == balance_after + actual_cost

        # Inv 4 (no duplicate idempotency_keys) implicitly verifies the refund key
        # isn't colliding with the original debit key (which is str(request_id),
        # not f"refund-{request_id}").
        await assert_financial_invariants(db_session, user_id)
    finally:
        await _purge_user_by_id(db_session, user_id)


# ── Scenario 5 ──────────────────────────────────────────────────────────────


async def test_scenario_5_full_lifecycle_credit_reserve_finalize(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    user = await _make_user(db_session, "integ_s5@example.com", 0)
    conv = await _make_conversation(db_session, user.id)
    user_id = user.id
    conv_id = conv.id
    try:
        # 1) Top-up credit.
        topup_amount = 200_000_000
        await credit_balance(
            db_session,
            user_id=user_id,
            amount_micro_rub=topup_amount,
            idempotency_key=f"topup-{uuid.uuid4()}",
            description="Top-up via YooKassa",
        )
        await db_session.commit()

        # 2) Run one chat request.
        await _drain(process_chat_request(
            user_id=user_id,
            conversation_id=conv_id,
            message_content="Привет",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=_ok_proxy(),
        ))

        db_session.expire_all()
        balance = (
            await db_session.execute(
                text("SELECT balance_micro_rub FROM users WHERE id = :uid"),
                {"uid": user_id},
            )
        ).scalar_one()
        reserved = (
            await db_session.execute(
                text("SELECT reserved_micro_rub FROM users WHERE id = :uid"),
                {"uid": user_id},
            )
        ).scalar_one()
        debit_amount = (
            await db_session.execute(
                text(
                    "SELECT amount_micro_rub FROM transactions "
                    "WHERE user_id = :uid AND type = 'debit' LIMIT 1"
                ),
                {"uid": user_id},
            )
        ).scalar_one()
        actual_cost = -debit_amount
        assert balance == topup_amount - actual_cost
        assert reserved == 0  # nothing held after finalization

        # Two transactions total: +topup, -actual_cost.
        txn_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM transactions WHERE user_id = :uid"),
                {"uid": user_id},
            )
        ).scalar_one()
        assert txn_count == 2

        # Active reservations after finalization: none.
        active_res = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM reservations "
                    "WHERE user_id = :uid AND status = 'active'"
                ),
                {"uid": user_id},
            )
        ).scalar_one()
        assert active_res == 0

        await assert_financial_invariants(db_session, user_id)
    finally:
        await _purge_user_by_id(db_session, user_id)
