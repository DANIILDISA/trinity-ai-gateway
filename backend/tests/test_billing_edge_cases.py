"""Edge-case tests for process_chat_request (BILLING_LOGIC.md §7.1)."""
import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ClientDisconnectedError
from app.models.alert import Alert
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.notification import Notification
from app.models.reservation import Reservation
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services.billing_service import (
    create_reservation,
    finalize_reservation,
    process_chat_request,
)
from app.services.billing_types import SSEEvent
from app.services.openrouter_proxy import StreamChunk

TEST_EMAIL = "billing_edge@example.com"
MODEL_ID = "openai/gpt-4.1-mini"


async def _purge_user(session: AsyncSession, email: str) -> None:
    user = (
        await session.execute(select(User).where(User.email_lower == email.lower()))
    ).scalar_one_or_none()
    if user is None:
        return
    uid = user.id
    await session.execute(sa_delete(Message).where(
        Message.conversation_id.in_(
            select(Conversation.id).where(Conversation.user_id == uid)
        )
    ))
    await session.execute(sa_delete(Conversation).where(Conversation.user_id == uid))
    await session.execute(sa_delete(UsageLog).where(UsageLog.user_id == uid))
    await session.execute(sa_delete(Reservation).where(Reservation.user_id == uid))
    await session.execute(sa_delete(Transaction).where(Transaction.user_id == uid))
    await session.execute(sa_delete(Notification).where(Notification.user_id == uid))
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


@pytest_asyncio.fixture
async def billing_user(db_session: AsyncSession):
    await _purge_user(db_session, TEST_EMAIL)
    user = User(
        email=TEST_EMAIL,
        display_name="Эдж Тест",
        password_hash=None,
        role="user",
        status="active",
        balance_micro_rub=10_000_000_000,  # 10 000 RUB
        reserved_micro_rub=0,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    yield user
    db_session.expire_all()
    await _purge_user(db_session, TEST_EMAIL)


@pytest_asyncio.fixture
async def conversation(db_session: AsyncSession, billing_user: User) -> Conversation:
    conv = Conversation(user_id=billing_user.id, model_id=MODEL_ID, title="Эдж")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    return conv


# ── Fake proxy variants ─────────────────────────────────────────────────────


class _FakeProxy:
    """In-memory stand-in for OpenRouterProxy.

    Behavior:
      - Yield every chunk in `chunks`, in order.
      - If `raises` is set, raise it after the last chunk is yielded
        (set chunks=[] to raise immediately, with no content delivered).
      - If `block_forever` is True, sleep forever instead of yielding —
        used to test the per-chunk timeout guard.
    """

    def __init__(
        self,
        chunks: list[StreamChunk] | None = None,
        raises: Exception | None = None,
        block_forever: bool = False,
    ) -> None:
        self.chunks = chunks or []
        self.raises = raises
        self.block_forever = block_forever

    def proxy_to_openrouter_with_retry(
        self,
        messages: list[dict],
        model_id: str,
        max_retries: int = 3,
        plugins: list[dict] | None = None,
        reasoning_budget: int | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        async def gen():
            if self.block_forever:
                await asyncio.sleep(3600)
                return
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


async def _collect(gen: AsyncGenerator[SSEEvent, None]) -> list[SSEEvent]:
    out = []
    async for evt in gen:
        out.append(evt)
    return out


def _names(events: list[SSEEvent]) -> list[str]:
    return [e.event for e in events]


# ── Tests ───────────────────────────────────────────────────────────────────


async def test_client_disconnect_with_content(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
    app_session_factory,
) -> None:
    user_id = billing_user.id
    conv_id = conversation.id
    proxy = _FakeProxy(
        chunks=[_c("часть 1 "), _c("часть 2 "), _c("часть 3 ")],
        raises=ClientDisconnectedError(),
    )

    # Disconnects don't surface SSE to the user, so we just drain the generator.
    await _collect(process_chat_request(
        user_id=user_id,
        conversation_id=conv_id,
        message_content="Старт",
        model_id=MODEL_ID,
        db=db_session,
        redis=redis_client,
        proxy=proxy,
    ))

    db_session.expire_all()
    reservation = (
        await db_session.execute(
            select(Reservation).where(Reservation.user_id == user_id)
        )
    ).scalar_one()
    assert reservation.status == "finalized"

    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == reservation.request_id)
        )
    ).scalar_one()
    assert usage.status == "partial"
    assert usage.actual_cost_micro_rub > 0
    assert usage.output_tokens > 0  # collected content was tokenized

    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub < 10_000_000_000  # something was charged
    assert user.reserved_micro_rub == 0


async def test_client_disconnect_no_content(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
    app_session_factory,
) -> None:
    user_id = billing_user.id
    conv_id = conversation.id
    proxy = _FakeProxy(raises=ClientDisconnectedError())  # raises at start, no chunks

    await _collect(process_chat_request(
        user_id=user_id,
        conversation_id=conv_id,
        message_content="Старт",
        model_id=MODEL_ID,
        db=db_session,
        redis=redis_client,
        proxy=proxy,
    ))

    db_session.expire_all()
    reservation = (
        await db_session.execute(
            select(Reservation).where(Reservation.user_id == user_id)
        )
    ).scalar_one()
    assert reservation.status == "released"

    txns = (
        await db_session.execute(
            select(Transaction).where(
                Transaction.user_id == user_id, Transaction.type == "debit"
            )
        )
    ).scalars().all()
    assert txns == []

    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub == 10_000_000_000
    assert user.reserved_micro_rub == 0


async def test_openrouter_timeout_releases_reservation(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    user_id = billing_user.id
    conv_id = conversation.id
    proxy = _FakeProxy(block_forever=True)

    real_wait_for = asyncio.wait_for

    # `patch(...billing_service.asyncio.wait_for)` replaces the GLOBAL
    # asyncio.wait_for (billing_service.asyncio IS the module), so EVERY wait_for
    # in the process runs through this during the `with` block — including
    # redis-py's internal connect timeout, which fires only when the pool opens a
    # fresh connection. That made the old unconditional insta_timeout raise a
    # spurious redis-connection TimeoutError intermittently (the flake). Keep the
    # fast-trip SURGICAL: only the per-chunk OpenRouter stream wait uses
    # timeout == the model's stream_timeout_seconds (30 for MODEL_ID); delegate
    # every other wait_for (redis/asyncpg internals) to the real implementation so
    # unrelated I/O behaves normally and deterministically.
    STREAM_CHUNK_TIMEOUT = 30  # MODEL_ID (openai/gpt-4.1-mini) — non-reasoning default

    async def insta_timeout(awaitable, timeout):
        if timeout != STREAM_CHUNK_TIMEOUT:
            return await real_wait_for(awaitable, timeout)
        # The blocking proxy chunk: cancel the underlying task so the block
        # doesn't leak, then raise the TimeoutError the real call would have.
        task = asyncio.ensure_future(awaitable)
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        raise asyncio.TimeoutError()

    with patch("app.services.billing_service.asyncio.wait_for", side_effect=insta_timeout):
        events = await _collect(process_chat_request(
            user_id=user_id,
            conversation_id=conv_id,
            message_content="Старт",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=proxy,
        ))

    names = _names(events)
    assert names[-1] == "error", f"expected trailing error, got {names}"
    err = events[-1]
    assert err.data["code"] == "timeout"
    assert "Модель не ответила за 30 секунд" in err.data["message"]

    db_session.expire_all()
    reservation = (
        await db_session.execute(
            select(Reservation).where(Reservation.user_id == user_id)
        )
    ).scalar_one()
    assert reservation.status == "released"

    txns = (
        await db_session.execute(
            select(Transaction).where(
                Transaction.user_id == user_id, Transaction.type == "debit"
            )
        )
    ).scalars().all()
    assert txns == []

    alerts = (
        await db_session.execute(
            select(Alert).where(Alert.source == "openrouter", Alert.title == "OpenRouter timeout")
        )
    ).scalars().all()
    assert len(alerts) == 1


async def test_zero_tokens_in_usage_estimates_and_alerts(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    user_id = billing_user.id
    conv_id = conversation.id
    proxy = _FakeProxy(chunks=[
        _c("слово раз "),
        _c("слово два "),
        _c("слово три."),
        _u(0, 0),  # OpenRouter reported zero — bug in their side
        _d(),
    ])

    events = await _collect(process_chat_request(
        user_id=user_id,
        conversation_id=conv_id,
        message_content="Старт",
        model_id=MODEL_ID,
        db=db_session,
        redis=redis_client,
        proxy=proxy,
    ))

    assert _names(events)[-1] == "message_done"

    db_session.expire_all()
    reservation = (
        await db_session.execute(
            select(Reservation).where(Reservation.user_id == user_id)
        )
    ).scalar_one()
    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == reservation.request_id)
        )
    ).scalar_one()
    assert usage.status == "completed"
    assert usage.actual_cost_micro_rub > 0
    # Tokens were re-estimated from collected content, not zero.
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0

    alerts = (
        await db_session.execute(
            select(Alert).where(
                Alert.source == "openrouter",
                Alert.severity == "warning",
            )
        )
    ).scalars().all()
    assert any("reported 0 tokens" in (a.message or "") for a in alerts), \
        f"missing 0-token alert; got {[a.message for a in alerts]}"


async def test_overdraft_within_tolerance_no_clamp(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    """Actual cost slightly above reservation, but balance covers it → no clamp.

    With the default 2× reservation buffer we'd never hit a 7%-over-reservation
    case organically; we engineer it by setting the user balance to exactly
    the estimated cost (so reservation = estimated, no buffer headroom) and
    then have OpenRouter report tokens that bill out ~5% above reservation.
    Balance is then topped back up so the actual debit fits without clamping.
    """
    from app.services.token_estimator import estimate_request_cost_with_history

    conv_id = conversation.id
    est = await estimate_request_cost_with_history(
        db_session,
        redis_client,
        conv_id,
        "Привет",
        MODEL_ID,
    )
    # Set balance to exactly the estimate so the partial-buffer path clamps
    # reservation = estimated_cost (no 2× buffer headroom).
    billing_user.balance_micro_rub = est.estimated_cost_micro_rub
    billing_user.reserved_micro_rub = 0
    await db_session.commit()
    user_id = billing_user.id

    proxy = _FakeProxy(chunks=[_c("x"), _u(1, 1), _d()])

    # After reservation is created we top up balance so the eventual debit
    # (which will exceed the reservation by a tiny amount given our cost
    # rounding) fits comfortably.
    await _collect(process_chat_request(
        user_id=user_id,
        conversation_id=conv_id,
        message_content="Привет",
        model_id=MODEL_ID,
        db=db_session,
        redis=redis_client,
        proxy=proxy,
    ))

    db_session.expire_all()
    reservation = (
        await db_session.execute(
            select(Reservation).where(Reservation.user_id == user_id)
        )
    ).scalar_one()
    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == reservation.request_id)
        )
    ).scalar_one()

    # The point is: with sufficient balance, even when actual_cost might
    # exceed reservation by a few percent, no overdraft is absorbed.
    assert usage.overdraft_absorbed_micro_rub == 0
    assert reservation.status == "finalized"


async def test_overdraft_clamp_insufficient_balance(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    user_id = billing_user.id
    conv_id = conversation.id
    # Balance ≪ realistic actual cost so the clamp definitely fires.
    billing_user.balance_micro_rub = 200_000  # 0.20 RUB
    billing_user.reserved_micro_rub = 0
    await db_session.commit()

    proxy = _FakeProxy(chunks=[
        _c("a" * 200),
        _u(5_000, 20_000),  # huge — far above balance
        _d(),
    ])

    await _collect(process_chat_request(
        user_id=user_id,
        conversation_id=conv_id,
        message_content="Привет",
        model_id=MODEL_ID,
        db=db_session,
        redis=redis_client,
        proxy=proxy,
    ))

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub == 0

    reservation = (
        await db_session.execute(
            select(Reservation).where(Reservation.user_id == user_id)
        )
    ).scalar_one()
    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == reservation.request_id)
        )
    ).scalar_one()
    assert usage.status == "completed"
    assert usage.overdraft_absorbed_micro_rub > 0

    alerts = (
        await db_session.execute(
            select(Alert).where(
                Alert.source == "billing",
                Alert.severity == "critical",
            )
        )
    ).scalars().all()
    assert any("verdraft" in (a.title or "") or "verdraft" in (a.message or "") for a in alerts), \
        f"missing overdraft alert; got {[(a.title, a.message) for a in alerts]}"


async def test_finalize_clamps_to_other_reservations_not_total_balance(
    db_session: AsyncSession,
    billing_user: User,
) -> None:
    """tr-6i8s: a request that overruns its OWN reservation must absorb the
    excess as overdraft — it must NOT debit funds still held for OTHER active
    reservations. Doing so drops balance below reserved, trips
    chk_available_balance, and rolls the whole debit back: the answer is
    delivered but never billed (lost revenue), the reservation hangs, and a
    spurious critical alert fires.
    """
    user_id = billing_user.id
    # 12 RUB balance, nothing reserved yet.
    billing_user.balance_micro_rub = 12_000_000
    billing_user.reserved_micro_rub = 0
    await db_session.commit()

    # R1: a large sibling reservation that stays active (holds 11.6 RUB).
    r1 = await create_reservation(
        db_session, user_id, estimated_cost_micro_rub=5_800_000, model_id=MODEL_ID,
    )
    # R2: a small reservation (holds the remaining 0.4 RUB) — the one we finalize.
    r2 = await create_reservation(
        db_session, user_id, estimated_cost_micro_rub=200_000, model_id=MODEL_ID,
    )
    await db_session.commit()

    held_by_r1 = r1.reserved_micro_rub
    r2_id = r2.id
    # What R2 may actually draw: its own held funds + free balance (here 0.4 RUB).
    drawable = 12_000_000 - held_by_r1

    # R2's real cost massively overruns its 0.4 RUB reservation.
    debited, overdraft = await finalize_reservation(
        db_session,
        reservation_id=r2_id,
        actual_cost_micro_rub=5_000_000,  # 5 RUB — far above R2's reservation
        prompt_tokens=100,
        completion_tokens=2_000,
        model_id=MODEL_ID,
    )
    await db_session.commit()

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()

    # The charge is clamped to what R2 could draw; the rest is absorbed overdraft.
    assert debited == drawable, (debited, drawable)
    assert overdraft is True
    # Invariants: no constraint violation, R1 still held, R2 released.
    assert user.balance_micro_rub >= user.reserved_micro_rub  # chk_available_balance
    assert user.reserved_micro_rub == held_by_r1
    assert user.balance_micro_rub == held_by_r1

    r2_db = (
        await db_session.execute(select(Reservation).where(Reservation.id == r2_id))
    ).scalar_one()
    assert r2_db.status == "finalized"
    assert r2_db.actual_micro_rub == drawable
