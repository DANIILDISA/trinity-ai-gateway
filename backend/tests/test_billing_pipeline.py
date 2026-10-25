"""End-to-end billing pipeline tests for process_chat_request (BILLING_LOGIC.md §2.2)."""
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InsufficientFundsError, OpenRouterServerError
from app.models.alert import Alert
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.model_pricing import ModelPricing
from app.models.notification import Notification
from app.models.reservation import Reservation
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services.billing_service import process_chat_request
from app.services.billing_types import SSEEvent
from app.services.openrouter_proxy import StreamChunk

TEST_EMAIL = "billing_pipeline@example.com"
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
    """Strip any billing/openrouter alerts before each test for clean assertions."""
    await db_session.execute(
        sa_delete(Alert).where(Alert.source.in_(["billing", "openrouter", "rate_limiter"]))
    )
    await db_session.commit()
    yield
    await db_session.execute(
        sa_delete(Alert).where(Alert.source.in_(["billing", "openrouter", "rate_limiter"]))
    )
    await db_session.commit()


@pytest_asyncio.fixture
async def billing_user(db_session: AsyncSession):
    await _purge_user(db_session, TEST_EMAIL)
    user = User(
        email=TEST_EMAIL,
        display_name="Пайплайн Тест",
        password_hash=None,
        role="user",
        status="active",
        balance_micro_rub=10_000_000_000,  # 10000 RUB — plenty of headroom
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
    conv = Conversation(user_id=billing_user.id, model_id=MODEL_ID, title="Тест")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    return conv


@pytest_asyncio.fixture(autouse=True)
async def _clear_rate_limit(redis_client: Redis):
    """Don't let leftover sliding-window state from other tests trip checks."""
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


# ── Mock proxies ────────────────────────────────────────────────────────────


class _FakeProxy:
    """In-memory OpenRouterProxy stand-in.

    Yields whatever the test supplied; raises if `raises=` is set when the
    consumer starts iterating. Matches the duck-typed interface used by the
    orchestrator: only `.proxy_to_openrouter_with_retry(messages, model_id)`.
    """

    def __init__(
        self,
        chunks: list[StreamChunk] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.chunks = chunks or []
        self.raises = raises
        self.captured_messages: list[dict] | None = None
        self.captured_plugins: list[dict] | None = None

    def proxy_to_openrouter_with_retry(
        self,
        messages: list[dict],
        model_id: str,
        max_retries: int = 3,
        plugins: list[dict] | None = None,
        reasoning_budget: int | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.captured_messages = messages
        self.captured_plugins = plugins
        self.captured_reasoning_budget = reasoning_budget

        async def gen():
            if self.raises is not None:
                raise self.raises
            for chunk in self.chunks:
                yield chunk

        return gen()


def _content(text: str) -> StreamChunk:
    return StreamChunk(type="content", delta=text)


def _usage(input_tokens: int, output_tokens: int) -> StreamChunk:
    return StreamChunk(type="usage", input_tokens=input_tokens, output_tokens=output_tokens)


def _usage_with_id(input_tokens: int, output_tokens: int, gen_id: str) -> StreamChunk:
    return StreamChunk(
        type="usage",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        openrouter_request_id=gen_id,
    )


def _done() -> StreamChunk:
    return StreamChunk(type="done")


async def _collect_events(gen: AsyncGenerator[SSEEvent, None]) -> list[SSEEvent]:
    out: list[SSEEvent] = []
    async for evt in gen:
        out.append(evt)
    return out


def _event_names(events: list[SSEEvent]) -> list[str]:
    return [e.event for e in events]


# ── Tests ───────────────────────────────────────────────────────────────────


async def test_happy_path_finalizes_and_charges(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    user_id = billing_user.id
    conv_id = conversation.id
    proxy = _FakeProxy(chunks=[
        _content("Привет, "),
        _content("это ответ."),
        _usage(input_tokens=42, output_tokens=18),
        _done(),
    ])

    events = await _collect_events(process_chat_request(
        user_id=user_id,
        conversation_id=conv_id,
        message_content="Здравствуй!",
        model_id=MODEL_ID,
        db=db_session,
        redis=redis_client,
        proxy=proxy,
    ))

    names = _event_names(events)
    assert names[0] == "message_start"
    assert names[-1] == "message_done"
    assert "content_delta" in names
    # context_truncated should NOT appear for a single-message conversation.
    assert "context_truncated" not in names

    db_session.expire_all()
    reservation = (
        await db_session.execute(
            select(Reservation).where(Reservation.user_id == user_id)
        )
    ).scalar_one()
    assert reservation.status == "finalized"
    assert reservation.actual_micro_rub is not None
    assert reservation.actual_micro_rub > 0

    txns = (
        await db_session.execute(
            select(Transaction).where(
                Transaction.user_id == user_id,
                Transaction.type == "debit",
            )
        )
    ).scalars().all()
    assert len(txns) == 1
    assert txns[0].amount_micro_rub < 0
    assert txns[0].idempotency_key == str(reservation.request_id)

    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == reservation.request_id)
        )
    ).scalar_one()
    assert usage.status == "completed"
    assert usage.input_tokens == 42
    assert usage.output_tokens == 18
    assert usage.conversation_id == conv_id
    # All 6 timing fields populated (acceptance criterion).
    assert usage.estimation_ms is not None
    assert usage.reservation_ms is not None
    assert usage.openrouter_ttfb_ms is not None
    assert usage.openrouter_total_ms is not None
    assert usage.finalization_ms is not None
    assert usage.total_ms is not None

    # message_done contains the required fields.
    done = events[-1]
    assert done.event == "message_done"
    assert "actual_cost" in done.data
    assert "balance_after" in done.data
    assert done.data["tokens_used"] == {"input": 42, "output": 18, "reasoning": 0}

    # Only the assistant message: the user-message persistence is the chat-send
    # endpoint's responsibility (Task 6.3); process_chat_request is only
    # responsible for the assistant response.
    msgs = (
        await db_session.execute(
            select(Message).where(Message.conversation_id == conv_id)
        )
    ).scalars().all()
    assert len(msgs) == 1
    assert msgs[0].role == "assistant"


async def test_openrouter_error_releases_reservation(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    user_id = billing_user.id
    conv_id = conversation.id
    proxy = _FakeProxy(raises=OpenRouterServerError())

    events = await _collect_events(process_chat_request(
        user_id=user_id,
        conversation_id=conv_id,
        message_content="Привет",
        model_id=MODEL_ID,
        db=db_session,
        redis=redis_client,
        proxy=proxy,
    ))

    names = _event_names(events)
    assert "error" in names, f"expected error event, got {names}"
    assert names[-1] == "error"

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
                Transaction.user_id == user_id,
                Transaction.type == "debit",
            )
        )
    ).scalars().all()
    assert txns == []

    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == reservation.request_id)
        )
    ).scalar_one()
    assert usage.status == "failed"

    # User balance untouched, reservation released.
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub == 10_000_000_000
    assert user.reserved_micro_rub == 0


async def test_empty_completion_releases_without_charge(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    """tr-34mj (E): a 'completed' stream that delivered ZERO content (no content
    deltas, no reasoning, 0 output tokens) must NOT bill the user for input — it
    is released like a failure per BILLING_LOGIC 'no content delivered -> release'.
    Prevents charging full input (e.g. 90k tokens) for an empty/rejected generation.
    """
    user_id = billing_user.id
    conv_id = conversation.id
    # Large input, NO content/reasoning, 0 output tokens — the empty-Opus shape.
    proxy = _FakeProxy(chunks=[
        _usage_with_id(input_tokens=90_000, output_tokens=0, gen_id="gen-empty"),
        _done(),
    ])

    events = await _collect_events(process_chat_request(
        user_id=user_id,
        conversation_id=conv_id,
        message_content="...",
        model_id=MODEL_ID,
        db=db_session,
        redis=redis_client,
        proxy=proxy,
    ))

    # An empty generation surfaces as an error, not a silent charge.
    assert _event_names(events)[-1] == "error"

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
    assert txns == []  # NOT charged for the empty generation

    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == reservation.request_id)
        )
    ).scalar_one()
    assert usage.status == "failed"

    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.balance_micro_rub == 10_000_000_000  # untouched
    assert user.reserved_micro_rub == 0


async def test_insufficient_funds_rejected_before_reservation(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    user_id = billing_user.id
    conv_id = conversation.id
    # Drain the seeded balance — keep just 5 μ₽ available.
    billing_user.balance_micro_rub = 5
    await db_session.commit()

    proxy = _FakeProxy(chunks=[_done()])

    with pytest.raises(InsufficientFundsError):
        async for _ in process_chat_request(
            user_id=user_id,
            conversation_id=conv_id,
            message_content="Привет",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=proxy,
        ):
            pass

    await db_session.rollback()
    db_session.expire_all()

    reservations = (
        await db_session.execute(
            select(Reservation).where(Reservation.user_id == user_id)
        )
    ).scalars().all()
    assert reservations == []

    # Proxy was never invoked.
    assert proxy.captured_messages is None


async def test_partial_buffer_clamps_reservation(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    """Balance between estimated_cost and 2× buffer → reservation clamped to available."""
    user_id = billing_user.id
    conv_id = conversation.id
    # First, find out the estimated_cost for a tiny prompt so we can set a balance
    # in the partial-buffer band. Use the same history-aware estimator the
    # pipeline uses (conversation has no prior usage_logs yet → deterministic).
    from app.services.token_estimator import estimate_request_cost_with_history

    est = await estimate_request_cost_with_history(
        db_session,
        redis_client,
        conv_id,
        "Привет",
        MODEL_ID,
    )
    # available between est and 2× est: pick estimated + 10%.
    target_balance = int(est.estimated_cost_micro_rub * 1.1)
    assert target_balance < est.estimated_cost_micro_rub * 2  # sanity
    billing_user.balance_micro_rub = target_balance
    billing_user.reserved_micro_rub = 0
    await db_session.commit()

    proxy = _FakeProxy(chunks=[_content("ok"), _usage(5, 1), _done()])

    await _collect_events(process_chat_request(
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
    # Reservation clamped at available balance (not at 2× estimated).
    assert reservation.reserved_micro_rub == target_balance
    # And the request still completed successfully.
    assert reservation.status == "finalized"


async def test_context_truncation_emits_event(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = billing_user.id
    conv_id = conversation.id

    # The dev DB may carry the real gpt-4.1-mini context window (~1M tokens),
    # which a padded history can't realistically overflow. Pin the pricing row
    # this request sees to a 128_000 window via a detached (non-session) copy,
    # so nothing is written to the DB. The real row is left untouched.
    import app.services.billing_service as billing_module
    from sqlalchemy import inspect as sa_inspect

    _orig_get_pricing = billing_module._get_model_pricing_row

    async def _pinned_pricing(db: AsyncSession, model_id: str) -> ModelPricing:
        row = await _orig_get_pricing(db, model_id)
        cols = {c.key: getattr(row, c.key) for c in sa_inspect(ModelPricing).columns}
        cols["max_context_tokens"] = 128_000
        return ModelPricing(**cols)

    monkeypatch.setattr(billing_module, "_get_model_pricing_row", _pinned_pricing)

    # 128_000-token window — pad a big history that overflows it.
    big_word = "длинный " * 1000  # ~1.4k tokens per message
    for i in range(120):
        db_session.add(Message(
            conversation_id=conv_id,
            role="user" if i % 2 == 0 else "assistant",
            content=f"{big_word} {i}",
        ))
    await db_session.commit()

    proxy = _FakeProxy(chunks=[_content("hi"), _usage(50, 1), _done()])
    events = await _collect_events(process_chat_request(
        user_id=user_id,
        conversation_id=conv_id,
        message_content="свежее сообщение",
        model_id=MODEL_ID,
        db=db_session,
        redis=redis_client,
        proxy=proxy,
    ))

    names = _event_names(events)
    assert "context_truncated" in names, f"expected truncation, got {names}"
    # context_truncated must come immediately after message_start.
    assert names[0] == "message_start"
    assert names[1] == "context_truncated"
    trunc = events[1]
    assert trunc.data["original_tokens"] > trunc.data["truncated_to_tokens"]
    assert trunc.data["messages_kept"] > 0


async def test_overdraft_clamps_and_alerts(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    user_id = billing_user.id
    conv_id = conversation.id
    # Tiny balance so even a moderate token count overdraws.
    billing_user.balance_micro_rub = 200_000  # 0.20 RUB
    billing_user.reserved_micro_rub = 0
    await db_session.commit()

    # Massive output → actual cost will exceed the balance, forcing the clamp.
    proxy = _FakeProxy(chunks=[
        _content("a" * 100),
        _usage(input_tokens=1000, output_tokens=10_000),
        _done(),
    ])

    await _collect_events(process_chat_request(
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
    assert user.reserved_micro_rub == 0

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
    assert usage.overdraft_absorbed_micro_rub > 0
    assert usage.actual_cost_micro_rub <= 200_000

    alerts = (
        await db_session.execute(
            select(Alert).where(
                Alert.source == "billing",
                Alert.severity == "critical",
            )
        )
    ).scalars().all()
    assert any("verdraft" in (a.title or "") or "verdraft" in (a.message or "") for a in alerts), \
        f"expected an overdraft alert, got {[(a.title, a.message) for a in alerts]}"


async def test_sse_event_order(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    user_id = billing_user.id
    conv_id = conversation.id
    proxy = _FakeProxy(chunks=[
        _content("часть 1 "),
        _content("часть 2 "),
        _content("часть 3"),
        _usage(input_tokens=12, output_tokens=8),
        _done(),
    ])

    events = await _collect_events(process_chat_request(
        user_id=user_id,
        conversation_id=conv_id,
        message_content="Старт",
        model_id=MODEL_ID,
        db=db_session,
        redis=redis_client,
        proxy=proxy,
    ))

    names = _event_names(events)
    # First must be message_start.
    assert names[0] == "message_start"
    # Last must be message_done.
    assert names[-1] == "message_done"
    # Every content_delta must come strictly before message_done.
    last_delta_idx = max(i for i, n in enumerate(names) if n == "content_delta")
    assert last_delta_idx < names.index("message_done")
    # Three delta events (one per content chunk).
    assert names.count("content_delta") == 3


async def test_free_model_reserves_zero_and_streams(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    """Free models price at 0, so the estimated cost — and reservation — is 0.

    Regression for the empty-bubble bug: the reservation INSERT must not be
    rejected by the reserved_micro_rub check, and the request must finalize
    with nothing charged.
    """
    user_id = billing_user.id
    conv_id = conversation.id
    free_model_id = "test/free-model:free"
    db_session.add(ModelPricing(
        model_id=free_model_id,
        model_name="Free Test",
        provider="test",
        openrouter_input_price_micro_usd=0,
        openrouter_output_price_micro_usd=0,
        input_price_micro_rub=0,
        output_price_micro_rub=0,
        max_context_tokens=8192,
        max_output_tokens=4096,
        status="available",
        is_current=True,
    ))
    await db_session.commit()

    try:
        proxy = _FakeProxy(chunks=[
            _content("Привет"),
            _usage(input_tokens=5, output_tokens=2),
            _done(),
        ])
        events = await _collect_events(process_chat_request(
            user_id=user_id,
            conversation_id=conv_id,
            message_content="Старт",
            model_id=free_model_id,
            db=db_session,
            redis=redis_client,
            proxy=proxy,
        ))

        names = _event_names(events)
        assert "error" not in names, f"unexpected error event: {names}"
        assert names[-1] == "message_done"

        db_session.expire_all()
        reservation = (
            await db_session.execute(
                select(Reservation).where(Reservation.user_id == user_id)
            )
        ).scalar_one()
        assert reservation.reserved_micro_rub == 0
        assert reservation.status == "finalized"

        user = (
            await db_session.execute(select(User).where(User.id == user_id))
        ).scalar_one()
        assert user.balance_micro_rub == 10_000_000_000  # free → nothing charged
        assert user.reserved_micro_rub == 0

        message = (
            await db_session.execute(
                select(Message).where(
                    Message.conversation_id == conv_id,
                    Message.role == "assistant",
                )
            )
        ).scalar_one()
        assert message.content == "Привет"
    finally:
        await db_session.execute(
            sa_delete(ModelPricing).where(ModelPricing.model_id == free_model_id)
        )
        await db_session.commit()


# ── Web search plugin ─────────────────────────────────────────────────────────


async def _set_web_search_support(
    db: AsyncSession, model_id: str, value: bool
) -> bool:
    """Pin the current pricing row's supports_web_search flag; return the old value."""
    row = (
        await db.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == model_id,
                ModelPricing.is_current.is_(True),
            )
        )
    ).scalar_one()
    old = row.supports_web_search
    row.supports_web_search = value
    await db.commit()
    return old


async def test_web_search_plugin_added_when_enabled(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    old = await _set_web_search_support(db_session, MODEL_ID, True)
    try:
        proxy = _FakeProxy(chunks=[_content("ok"), _usage(10, 2), _done()])
        await _collect_events(process_chat_request(
            user_id=billing_user.id,
            conversation_id=conversation.id,
            message_content="Что нового?",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=proxy,
            web_search_enabled=True,
        ))
        assert proxy.captured_plugins == [{"id": "web"}]
    finally:
        await _set_web_search_support(db_session, MODEL_ID, old)


async def test_web_search_plugin_not_added_when_model_unsupported(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    old = await _set_web_search_support(db_session, MODEL_ID, False)
    try:
        proxy = _FakeProxy(chunks=[_content("ok"), _usage(10, 2), _done()])
        await _collect_events(process_chat_request(
            user_id=billing_user.id,
            conversation_id=conversation.id,
            message_content="Что нового?",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=proxy,
            web_search_enabled=True,
        ))
        # Requested but unsupported → plugin silently dropped, never forwarded.
        assert proxy.captured_plugins is None
    finally:
        await _set_web_search_support(db_session, MODEL_ID, old)


def _usage_with_cost(
    input_tokens: int, output_tokens: int, cost_usd: float
) -> StreamChunk:
    return StreamChunk(
        type="usage",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )


async def test_web_search_charges_on_cost_basis_including_surcharge(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    """Web-search adds a flat per-request surcharge OpenRouter folds into
    usage.cost but NOT into token counts. Mirrors the bug report (1679 in / 57
    out, cost $0.0051 dominated by the ~$0.005 search fee). The charge must be
    OpenRouter's USD cost × exchange rate × markup — NOT tokens × per-token
    price, which misses the surcharge and undercharges ~28×.
    """
    from app.core.constants import (
        calculate_token_cost_micro_rub,
        micro_usd_to_micro_rub,
        usd_float_to_micro_usd,
    )
    from app.services.tariff_service import _get_pricing_settings

    user_id = billing_user.id
    old = await _set_web_search_support(db_session, MODEL_ID, True)
    try:
        pricing = (
            await db_session.execute(
                select(ModelPricing).where(
                    ModelPricing.model_id == MODEL_ID,
                    ModelPricing.is_current.is_(True),
                )
            )
        ).scalar_one()
        # Capture prices now: expire_all() below would otherwise trigger a lazy
        # (sync) reload when we read them, which fails outside the async context.
        input_price = pricing.input_price_micro_rub
        output_price = pricing.output_price_micro_rub
        exchange_rate, markup = await _get_pricing_settings(db_session)

        # Tiny token bill, but usage.cost is dominated by the search surcharge.
        actual_input, actual_output, cost_usd = 1679, 57, 0.0051
        proxy = _FakeProxy(chunks=[
            _content("Свежие новости: ..."),
            _usage_with_cost(actual_input, actual_output, cost_usd),
            _done(),
        ])

        events = await _collect_events(process_chat_request(
            user_id=user_id,
            conversation_id=conversation.id,
            message_content="что нового?",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=proxy,
            web_search_enabled=True,
        ))
        assert _event_names(events)[-1] == "message_done"

        db_session.expire_all()
        usage = (
            await db_session.execute(
                select(UsageLog).where(UsageLog.user_id == user_id)
            )
        ).scalar_one()

        # Tokens still recorded as the ACTUAL counts (prior fix).
        assert usage.input_tokens == actual_input
        assert usage.output_tokens == actual_output

        # What token-only billing WOULD have charged (the buggy, too-low figure).
        token_only = (
            calculate_token_cost_micro_rub(actual_input, input_price)
            + calculate_token_cost_micro_rub(actual_output, output_price)
        )
        # The correct charge: OpenRouter cost × rate × markup (tokens + surcharge).
        cost_basis = micro_usd_to_micro_rub(
            usd_float_to_micro_usd(cost_usd), exchange_rate
        )
        markup_int = round(markup * 100)
        cost_with_markup = (cost_basis * markup_int + 99) // 100
        expected_cost = max(token_only, cost_with_markup)

        assert usage.actual_cost_micro_rub == expected_cost
        # The surcharge dominates → cost-based charge far exceeds token-only.
        assert cost_with_markup > token_only
        assert usage.actual_cost_micro_rub > token_only
        # OpenRouter's real USD cost (incl. surcharge) stored as the cost basis.
        assert usage.openrouter_cost_micro_usd == usd_float_to_micro_usd(cost_usd)

        # The debit transaction matches the cost-based charge.
        debit = (
            await db_session.execute(
                select(Transaction).where(
                    Transaction.user_id == user_id,
                    Transaction.type == "debit",
                )
            )
        ).scalar_one()
        assert debit.amount_micro_rub == -expected_cost
    finally:
        await _set_web_search_support(db_session, MODEL_ID, old)


def test_compute_actual_cost_surcharge_and_passthrough() -> None:
    """Unit-cover the cost helper's branches (no DB)."""
    from app.core.constants import calculate_token_cost_micro_rub
    from app.services.billing_service import compute_actual_cost_micro_rub

    # 1 RUB / 1M input tokens, 3 RUB / 1M output (already markup-baked prices).
    pricing = ModelPricing(
        model_id="x", model_name="x", provider="x",
        openrouter_input_price_micro_usd=0, openrouter_output_price_micro_usd=0,
        input_price_micro_rub=1_000_000, output_price_micro_rub=3_000_000,
        max_context_tokens=8192, max_output_tokens=4096,
        status="available", is_current=True,
    )
    rate = 82_500_000   # 82.50 RUB/USD
    markup = 1.25

    token_only = (
        calculate_token_cost_micro_rub(1000, 1_000_000)  # 1000 * 1 µ₽ = 1000
        + calculate_token_cost_micro_rub(100, 3_000_000)  # 100 * 3 µ₽ = 300
    )
    assert token_only == 1_300

    # Non-web-search: always token-only, even with a cost reported.
    assert compute_actual_cost_micro_rub(
        1000, 100, pricing, cost_usd=0.01,
        exchange_rate_micro_rub=rate, markup_multiplier=markup,
        web_search_enabled=False,
    ) == token_only

    # Web-search with no cost reported: fall back to token-only.
    assert compute_actual_cost_micro_rub(
        1000, 100, pricing, cost_usd=None,
        exchange_rate_micro_rub=rate, markup_multiplier=markup,
        web_search_enabled=True,
    ) == token_only

    # Web-search with a surcharge-bearing cost: cost × rate × markup, > token_only.
    cost_usd = 0.0051
    web = compute_actual_cost_micro_rub(
        1000, 100, pricing, cost_usd=cost_usd,
        exchange_rate_micro_rub=rate, markup_multiplier=markup,
        web_search_enabled=True,
    )
    cost_basis = (round(cost_usd * 1_000_000) * rate + 999_999) // 1_000_000
    expected = (cost_basis * 125 + 99) // 100
    assert web == expected
    assert web > token_only

    # max() floor: a cost basis below token-only never undercharges.
    assert compute_actual_cost_micro_rub(
        1000, 100, pricing, cost_usd=0.0000001,
        exchange_rate_micro_rub=rate, markup_multiplier=markup,
        web_search_enabled=True,
    ) == token_only


async def test_web_search_stored_in_usage_log(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    user_id = billing_user.id
    old = await _set_web_search_support(db_session, MODEL_ID, True)
    try:
        proxy = _FakeProxy(chunks=[_content("ok"), _usage(10, 2), _done()])
        await _collect_events(process_chat_request(
            user_id=user_id,
            conversation_id=conversation.id,
            message_content="Что нового?",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=proxy,
            web_search_enabled=True,
        ))

        db_session.expire_all()
        usage = (
            await db_session.execute(
                select(UsageLog).where(UsageLog.user_id == user_id)
            )
        ).scalar_one()
        assert usage.web_search_enabled is True
    finally:
        await _set_web_search_support(db_session, MODEL_ID, old)


async def test_completed_persists_openrouter_request_id(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    """The gen-… id must land in usage_logs.openrouter_request_id on success."""
    conv_id = conversation.id
    proxy = _FakeProxy(chunks=[
        StreamChunk(type="content", delta="hi", openrouter_request_id="gen-xyz"),
        _usage_with_id(42, 18, "gen-xyz"),
        _done(),
    ])

    await _collect_events(process_chat_request(
        user_id=billing_user.id,
        conversation_id=conv_id,
        message_content="Здравствуй!",
        model_id=MODEL_ID,
        db=db_session,
        redis=redis_client,
        proxy=proxy,
    ))

    db_session.expire_all()
    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.conversation_id == conv_id)
        )
    ).scalar_one()
    assert usage.openrouter_request_id == "gen-xyz"


# ── tr-sgz: completed finalize on a fresh session ────────────────────────────


async def test_completed_finalize_runs_on_fresh_session(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tr-sgz: the normal completed finalize must run on a FRESH session, never the
    request-scoped one (mirrors the abort-path guarantee, tr-04a) — so a request
    connection killed mid-stream can't strand the charge."""
    import app.services.billing_service as bs

    real = bs.finalize_reservation
    captured: dict = {}

    async def _spy(db, *a, **k):
        captured["db"] = db
        return await real(db, *a, **k)

    monkeypatch.setattr(bs, "finalize_reservation", _spy)

    proxy = _FakeProxy(chunks=[_content("ответ"), _usage(42, 18), _done()])
    await _collect_events(process_chat_request(
        user_id=billing_user.id, conversation_id=conversation.id, message_content="hi",
        model_id=MODEL_ID, db=db_session, redis=redis_client, proxy=proxy,
    ))

    assert "db" in captured, "finalize_reservation was not called"
    assert captured["db"] is not db_session  # finalize ran on a FRESH session


async def test_completed_finalize_survives_dead_request_connection(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
    engine,
) -> None:
    """tr-sgz headline: if the request connection dies mid-stream (here: its Postgres
    backend is terminated, exactly as idle_in_transaction_session_timeout would do),
    the streamed answer is still persisted AND billed on the fresh finalize session."""
    from sqlalchemy import text

    from app.core.database import get_session_factory

    user_id = billing_user.id
    conv_id = conversation.id

    # A dedicated "request" session we will poison — keeping the test's db_session
    # (used for assertions + fixture teardown) healthy.
    victim = get_session_factory()()

    async def _poison() -> None:
        pid = (await victim.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        async with engine.connect() as killer:
            await killer.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            await killer.commit()

    class _PoisonProxy:
        def proxy_to_openrouter_with_retry(self, messages, model_id, max_retries=3,
                                           plugins=None, reasoning_budget=None):
            async def gen():
                yield _content("ответ")
                yield _usage(42, 18)
                await _poison()      # request connection dies here, before STEP 6
                yield _done()
            return gen()

    try:
        events = await _collect_events(process_chat_request(
            user_id=user_id, conversation_id=conv_id, message_content="hi",
            model_id=MODEL_ID, db=victim, redis=redis_client, proxy=_PoisonProxy(),
        ))
    finally:
        try:
            await victim.close()
        except Exception:
            pass

    assert _event_names(events)[-1] == "message_done"

    db_session.expire_all()
    reservation = (await db_session.execute(
        select(Reservation).where(Reservation.user_id == user_id)
    )).scalar_one()
    assert reservation.status == "finalized"
    assert reservation.actual_micro_rub is not None and reservation.actual_micro_rub > 0

    msg = (await db_session.execute(
        select(Message).where(Message.conversation_id == conv_id, Message.role == "assistant")
    )).scalar_one()
    assert msg.content == "ответ"

    debit = (await db_session.execute(
        select(Transaction).where(Transaction.user_id == user_id, Transaction.type == "debit")
    )).scalar_one()
    assert debit.idempotency_key == str(reservation.request_id)


async def test_process_chat_request_through_real_proxy_persists_and_charges(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration regression (tr-sgz): drive process_chat_request through the REAL
    OpenRouterProxy (only the OpenAI network is mocked), so the proxy's transaction
    handling on the SHARED request session is actually exercised against STEP 6
    finalize. The window#1 `rollback()` bug — which expired the orchestrator's pricing
    ORM object → MissingGreenlet → lost answer + critical billing alert — was invisible
    to every `_FakeProxy` test because the fake never touches the DB session."""
    import app.services.openrouter_proxy as proxy_module
    from app.core.security import encrypt_key
    from app.models.api_key import ApiKey
    from app.services.openrouter_proxy import OpenRouterProxy

    user_id = billing_user.id
    conv_id = conversation.id

    db_session.add(ApiKey(
        label="mock", encrypted_key=encrypt_key("sk-or-mock-int12345"),
        key_suffix="int12345", status="active", is_primary=True,
    ))
    await db_session.commit()

    # Minimal fake of the streaming OpenAI SDK the real proxy consumes.
    class _Delta:
        def __init__(self, content=None):
            self.content = content
            self.reasoning = None
            self.reasoning_details = None
            self.tool_calls = None

    class _Choice:
        def __init__(self, content):
            self.delta = _Delta(content)

    class _Usage:
        def __init__(self):
            self.prompt_tokens = 63
            self.completion_tokens = 4
            self.cost = 0.0
            self.completion_tokens_details = None

    class _Chunk:
        def __init__(self, content=None, usage=None):
            self.id = "gen-int-test-123"
            self.choices = [_Choice(content)] if content is not None else []
            self.usage = usage

    class _Stream:
        def __aiter__(self):
            async def _gen():
                yield _Chunk(content="Paris.")
                yield _Chunk(usage=_Usage())
            return _gen()

    class _Completions:
        async def create(self, **_kw):
            return _Stream()

    class _Client:
        def __init__(self, **_kw):
            self.chat = type("_Chat", (), {"completions": _Completions()})()

    monkeypatch.setattr(proxy_module.openai, "AsyncOpenAI", _Client)

    events = await _collect_events(process_chat_request(
        user_id=user_id, conversation_id=conv_id, message_content="столица Франции?",
        model_id=MODEL_ID, db=db_session, redis=redis_client,
        proxy=OpenRouterProxy(db_session),
    ))

    # The whole point: the streamed answer must be persisted AND billed — STEP 6 ran
    # against a non-expired pricing object on the shared session.
    assert _event_names(events)[-1] == "message_done"

    db_session.expire_all()
    msg = (await db_session.execute(
        select(Message).where(Message.conversation_id == conv_id, Message.role == "assistant")
    )).scalar_one()
    assert msg.content == "Paris."
    reservation = (await db_session.execute(
        select(Reservation).where(Reservation.user_id == user_id)
    )).scalar_one()
    assert reservation.status == "finalized"
    debit = (await db_session.execute(
        select(Transaction).where(Transaction.user_id == user_id, Transaction.type == "debit")
    )).scalar_one()
    assert debit.amount_micro_rub < 0
    assert debit.idempotency_key == str(reservation.request_id)
