"""Billing tests for extended thinking (reasoning tokens).

Covers the cost helper (reasoning split out of output, billed at the input rate,
with a usage.cost floor) and the end-to-end pipeline (thinking_delta SSE,
reasoning-token + thinking-text persistence, correct ruble charge, and the
supports_reasoning gate).
"""
from collections.abc import AsyncGenerator

import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import calculate_token_cost_micro_rub
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.model_pricing import ModelPricing
from app.models.notification import Notification
from app.models.reservation import Reservation
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services.billing_service import (
    compute_actual_cost_micro_rub,
    process_chat_request,
)
from app.services.billing_types import SSEEvent
from app.services.openrouter_proxy import StreamChunk

TEST_EMAIL = "billing_thinking@example.com"
MODEL_ID = "openai/gpt-4.1-mini"


# ── Unit: compute_actual_cost_micro_rub ──────────────────────────────────────


def _pricing(input_price: int, output_price: int) -> ModelPricing:
    return ModelPricing(
        model_id="x", model_name="x", provider="x",
        openrouter_input_price_micro_usd=0, openrouter_output_price_micro_usd=0,
        input_price_micro_rub=input_price, output_price_micro_rub=output_price,
        max_context_tokens=8192, max_output_tokens=4096,
        status="available", is_current=True,
    )


def test_reasoning_tokens_billed_at_input_rate() -> None:
    """reasoning is split out of output and charged at the INPUT per-token rate."""
    pricing = _pricing(input_price=1_000_000, output_price=3_000_000)  # 1 µ₽ in, 3 µ₽ out
    # output_tokens INCLUDES reasoning (OpenRouter contract): 100 total, 40 reasoning.
    cost = compute_actual_cost_micro_rub(
        input_tokens=50,
        output_tokens=100,
        pricing=pricing,
        cost_usd=None,  # no floor
        exchange_rate_micro_rub=82_500_000,
        markup_multiplier=1.25,
        web_search_enabled=False,
        reasoning_tokens=40,
    )
    expected = (
        calculate_token_cost_micro_rub(50, 1_000_000)          # input: 50 µ₽
        + calculate_token_cost_micro_rub(100 - 40, 3_000_000)  # visible output: 180 µ₽
        + calculate_token_cost_micro_rub(40, 1_000_000)        # reasoning @ input: 40 µ₽
    )
    assert cost == expected
    # Sanity: cheaper than billing all output (incl. reasoning) at the output rate.
    all_output = (
        calculate_token_cost_micro_rub(50, 1_000_000)
        + calculate_token_cost_micro_rub(100, 3_000_000)
    )
    assert cost < all_output


def test_reasoning_zero_is_backward_compatible() -> None:
    """reasoning_tokens=0 yields the pre-feature token cost exactly."""
    pricing = _pricing(input_price=1_000_000, output_price=3_000_000)
    legacy = (
        calculate_token_cost_micro_rub(50, 1_000_000)
        + calculate_token_cost_micro_rub(100, 3_000_000)
    )
    assert compute_actual_cost_micro_rub(
        50, 100, pricing, cost_usd=None,
        exchange_rate_micro_rub=82_500_000, markup_multiplier=1.25,
        web_search_enabled=False, reasoning_tokens=0,
    ) == legacy


def test_reasoning_cost_floor_applies() -> None:
    """When reasoning>0 and usage.cost is reported, the charge is floored at
    OpenRouter's USD cost × rate × markup (operator-margin protection)."""
    pricing = _pricing(input_price=1_000_000, output_price=3_000_000)
    rate = 82_500_000
    markup = 1.25
    cost_usd = 0.01  # large relative to the tiny token bill → floor dominates

    token_cost = (
        calculate_token_cost_micro_rub(10, 1_000_000)
        + calculate_token_cost_micro_rub(20 - 15, 3_000_000)
        + calculate_token_cost_micro_rub(15, 1_000_000)
    )
    cost_basis = (round(cost_usd * 1_000_000) * rate + 999_999) // 1_000_000
    floor = (cost_basis * 125 + 99) // 100

    charged = compute_actual_cost_micro_rub(
        10, 20, pricing, cost_usd=cost_usd,
        exchange_rate_micro_rub=rate, markup_multiplier=markup,
        web_search_enabled=False, reasoning_tokens=15,
    )
    assert charged == max(token_cost, floor)
    assert charged == floor  # floor dominates here
    assert floor > token_cost


# ── E2E pipeline fixtures (mirror test_billing_pipeline) ──────────────────────


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


@pytest_asyncio.fixture
async def billing_user(db_session: AsyncSession):
    await _purge_user(db_session, TEST_EMAIL)
    user = User(
        email=TEST_EMAIL,
        display_name="Мышление Тест",
        password_hash=None,
        role="user",
        status="active",
        balance_micro_rub=10_000_000_000,
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


class _FakeProxy:
    """Captures reasoning_budget + plugins, yields the supplied chunks."""

    def __init__(self, chunks: list[StreamChunk] | None = None) -> None:
        self.chunks = chunks or []
        self.captured_reasoning_budget: object = "UNSET"
        self.captured_plugins: list[dict] | None = None

    def proxy_to_openrouter_with_retry(
        self,
        messages: list[dict],
        model_id: str,
        max_retries: int = 3,
        plugins: list[dict] | None = None,
        reasoning_budget: int | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.captured_plugins = plugins
        self.captured_reasoning_budget = reasoning_budget

        async def gen():
            for chunk in self.chunks:
                yield chunk

        return gen()


async def _collect(gen: AsyncGenerator[SSEEvent, None]) -> list[SSEEvent]:
    return [e async for e in gen]


async def _set_reasoning_support(db: AsyncSession, model_id: str, value: bool) -> bool:
    row = (
        await db.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == model_id,
                ModelPricing.is_current.is_(True),
            )
        )
    ).scalar_one()
    old = row.supports_reasoning
    row.supports_reasoning = value
    await db.commit()
    return old


# ── E2E tests ─────────────────────────────────────────────────────────────────


async def test_thinking_streams_persists_and_bills_input_rate(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    user_id = billing_user.id
    conv_id = conversation.id
    old = await _set_reasoning_support(db_session, MODEL_ID, True)
    try:
        pricing = (
            await db_session.execute(
                select(ModelPricing).where(
                    ModelPricing.model_id == MODEL_ID,
                    ModelPricing.is_current.is_(True),
                )
            )
        ).scalar_one()
        input_price = pricing.input_price_micro_rub
        output_price = pricing.output_price_micro_rub

        # output_tokens (60) INCLUDES reasoning (40) per the OpenRouter contract.
        proxy = _FakeProxy(chunks=[
            StreamChunk(type="reasoning", delta="Сначала подумаю: "),
            StreamChunk(type="reasoning", delta="9.11 < 9.9."),
            StreamChunk(type="content", delta="9.9 больше."),
            StreamChunk(
                type="usage", input_tokens=42, output_tokens=60, reasoning_tokens=40
            ),
            StreamChunk(type="done"),
        ])

        events = await _collect(process_chat_request(
            user_id=user_id,
            conversation_id=conv_id,
            message_content="Что больше, 9.9 или 9.11?",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=proxy,
            thinking_enabled=True,
            thinking_effort="high",
        ))

        names = [e.event for e in events]
        # thinking_delta events appear, before the final message_done.
        assert "thinking_delta" in names
        assert names[-1] == "message_done"
        thinking_text = "".join(
            e.data["delta"] for e in events if e.event == "thinking_delta"
        )
        assert thinking_text == "Сначала подумаю: 9.11 < 9.9."

        # high → 16384 budget reached the proxy.
        assert proxy.captured_reasoning_budget == 16384

        done = events[-1]
        assert done.data["tokens_used"] == {"input": 42, "output": 60, "reasoning": 40}

        db_session.expire_all()
        usage = (
            await db_session.execute(
                select(UsageLog).where(UsageLog.user_id == user_id)
            )
        ).scalar_one()
        assert usage.reasoning_tokens == 40
        assert usage.input_tokens == 42
        assert usage.output_tokens == 60

        # Charged at input rate for reasoning (no cost_usd → no floor).
        expected = (
            calculate_token_cost_micro_rub(42, input_price)
            + calculate_token_cost_micro_rub(60 - 40, output_price)
            + calculate_token_cost_micro_rub(40, input_price)
        )
        assert usage.actual_cost_micro_rub == expected

        # Thinking text + token split persisted on the assistant message.
        message = (
            await db_session.execute(
                select(Message).where(
                    Message.conversation_id == conv_id,
                    Message.role == "assistant",
                )
            )
        ).scalar_one()
        assert message.content == "9.9 больше."
        assert message.thinking == "Сначала подумаю: 9.11 < 9.9."
        assert message.reasoning_tokens == 40
    finally:
        await _set_reasoning_support(db_session, MODEL_ID, old)


async def test_thinking_request_pre_reserves_reasoning_budget(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    """A thinking request reserves the reasoning budget on top of the token
    estimate, so the cost-basis floor isn't absorbed as overdraft at finalize.

    Reservation = (token_estimate + surcharge) × buffer. We compute the token
    estimate independently for the (empty) conversation and the surcharge from
    the public helper, then assert the persisted reserved amount matches.
    """
    from app.services.billing_service import (
        RESERVATION_BUFFER_FACTOR,
        estimate_surcharge_micro_rub,
    )
    from app.services.token_estimator import estimate_request_cost_with_history

    user_id = billing_user.id
    conv_id = conversation.id
    message = "Что больше, 9.9 или 9.11?"
    old = await _set_reasoning_support(db_session, MODEL_ID, True)
    try:
        pricing = (
            await db_session.execute(
                select(ModelPricing).where(
                    ModelPricing.model_id == MODEL_ID,
                    ModelPricing.is_current.is_(True),
                )
            )
        ).scalar_one()

        # Token-only estimate for this (still empty) conversation — identical to
        # what process_chat_request computes before reserving.
        base = await estimate_request_cost_with_history(
            db_session, redis_client, conv_id, message, MODEL_ID,
            pricing.max_output_tokens,
        )
        surcharge = estimate_surcharge_micro_rub(
            pricing,
            web_search_enabled=False,
            reasoning_budget=16384,  # high effort
            exchange_rate_micro_rub=0,  # no web search → rate unused
            markup_multiplier=1.0,
        )
        assert surcharge > 0  # guards against a no-op fix

        proxy = _FakeProxy(chunks=[
            StreamChunk(type="reasoning", delta="…"),
            StreamChunk(type="content", delta="9.9 больше."),
            StreamChunk(type="usage", input_tokens=42, output_tokens=60, reasoning_tokens=40),
            StreamChunk(type="done"),
        ])
        await _collect(process_chat_request(
            user_id=user_id,
            conversation_id=conv_id,
            message_content=message,
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=proxy,
            thinking_enabled=True,
            thinking_effort="high",
        ))

        db_session.expire_all()
        # The reservation row keeps the amount that was held (balance huge → no clamp).
        reservation = (
            await db_session.execute(
                select(Reservation).where(Reservation.user_id == user_id)
            )
        ).scalar_one()
        expected = (
            base.estimated_cost_micro_rub + surcharge
        ) * RESERVATION_BUFFER_FACTOR
        assert reservation.reserved_micro_rub == expected
    finally:
        await _set_reasoning_support(db_session, MODEL_ID, old)


async def test_thinking_ignored_for_unsupported_model(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    user_id = billing_user.id
    conv_id = conversation.id
    old = await _set_reasoning_support(db_session, MODEL_ID, False)
    try:
        proxy = _FakeProxy(chunks=[
            StreamChunk(type="content", delta="Ответ."),
            StreamChunk(type="usage", input_tokens=10, output_tokens=5),
            StreamChunk(type="done"),
        ])
        events = await _collect(process_chat_request(
            user_id=user_id,
            conversation_id=conv_id,
            message_content="Привет",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=proxy,
            thinking_enabled=True,
            thinking_effort="high",
        ))

        names = [e.event for e in events]
        assert "thinking_delta" not in names
        assert names[-1] == "message_done"
        # No budget forwarded to the proxy when the model can't reason.
        assert proxy.captured_reasoning_budget is None

        db_session.expire_all()
        usage = (
            await db_session.execute(
                select(UsageLog).where(UsageLog.user_id == user_id)
            )
        ).scalar_one()
        assert usage.reasoning_tokens == 0
    finally:
        await _set_reasoning_support(db_session, MODEL_ID, old)


# ── Per-segment SSE translation (tr-cy0.3.6) ──────────────────────────────────


async def _run_segmented(
    db_session, redis_client, billing_user, conversation, chunks
):
    """Drive process_chat_request with reasoning-segment chunks; return events."""
    old = await _set_reasoning_support(db_session, MODEL_ID, True)
    try:
        proxy = _FakeProxy(chunks=chunks)
        return await _collect(process_chat_request(
            user_id=billing_user.id,
            conversation_id=conversation.id,
            message_content="вопрос",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=proxy,
            thinking_enabled=True,
            thinking_effort="high",
        ))
    finally:
        await _set_reasoning_support(db_session, MODEL_ID, old)


async def test_multisegment_emits_segment_and_summary_in_order(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    events = await _run_segmented(
        db_session, redis_client, billing_user, conversation,
        chunks=[
            StreamChunk(type="reasoning", delta="Анализирую вопросы. ",
                        segment_break=True, segment_index=0),
            StreamChunk(type="reasoning", delta="Детали анализа.",
                        segment_break=False, segment_index=0),
            StreamChunk(type="reasoning", delta="Готовлю ответ. ",
                        segment_break=True, segment_index=1),
            StreamChunk(type="reasoning", delta="Финальный разбор.",
                        segment_break=False, segment_index=1),
            StreamChunk(type="content", delta="Ответ."),
            StreamChunk(type="usage", input_tokens=42, output_tokens=60,
                        reasoning_tokens=40),
            StreamChunk(type="done"),
        ],
    )
    names = [e.event for e in events]

    # Segment 0 stays implicit (no thinking_segment); only the real boundary to
    # index 1 gets an explicit thinking_segment.
    segs = [e for e in events if e.event == "thinking_segment"]
    assert [e.data for e in segs] == [{"segment_id": "seg-1", "index": 1}]

    # Every segment gets a thinking_summary (seg-0 retro-titled at the boundary,
    # seg-1 titled at the reasoning→content transition).
    sums = [e for e in events if e.event == "thinking_summary"]
    assert [e.data["segment_id"] for e in sums] == ["seg-0", "seg-1"]
    assert sums[0].data["summary"] == "Анализирую вопросы"
    assert sums[1].data["summary"] == "Готовлю ответ"

    # Ordering: seg-0 summary precedes the seg-1 boundary; seg-1 summary lands
    # right before the first content_delta (the sync title never delays content).
    assert names.index("thinking_summary") < names.index("thinking_segment")
    i_last_sum = max(i for i, n in enumerate(names) if n == "thinking_summary")
    assert names[i_last_sum + 1] == "content_delta"
    assert names[-1] == "message_done"
    # All summaries arrive before message_done.
    assert i_last_sum < names.index("message_done")


async def test_single_segment_stream_is_legacy_identical(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    events = await _run_segmented(
        db_session, redis_client, billing_user, conversation,
        chunks=[
            StreamChunk(type="reasoning", delta="Думаю над задачей.",
                        segment_break=True, segment_index=0),
            StreamChunk(type="reasoning", delta=" Ещё мысли.",
                        segment_break=False, segment_index=0),
            StreamChunk(type="content", delta="Готово."),
            StreamChunk(type="usage", input_tokens=10, output_tokens=5),
            StreamChunk(type="done"),
        ],
    )
    names = [e.event for e in events]
    # A single segment emits NO new events — byte-identical to the legacy stream.
    assert "thinking_segment" not in names
    assert "thinking_summary" not in names
    thinking = "".join(e.data["delta"] for e in events if e.event == "thinking_delta")
    assert thinking == "Думаю над задачей. Ещё мысли."


async def test_provider_russian_summary_preferred_over_heuristic(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    events = await _run_segmented(
        db_session, redis_client, billing_user, conversation,
        chunks=[
            StreamChunk(type="reasoning", delta="english internal reasoning",
                        segment_break=True, segment_index=0),
            # Provider summary rides on a delta=None chunk and must still reach
            # the orchestrator (forwarded by stream_with_cost_monitoring).
            StreamChunk(type="reasoning", delta=None, segment_break=False,
                        segment_index=0, segment_summary="Классифицирую вопросы"),
            StreamChunk(type="reasoning", delta="more english here",
                        segment_break=True, segment_index=1),
            StreamChunk(type="content", delta="Answer."),
            StreamChunk(type="usage", input_tokens=10, output_tokens=5),
            StreamChunk(type="done"),
        ],
    )
    sums = [e for e in events if e.event == "thinking_summary"]
    assert sums[0].data["segment_id"] == "seg-0"
    assert sums[0].data["summary"] == "Классифицирую вопросы"


# ── Per-segment persistence (tr-cy0.3.7) ──────────────────────────────────────


async def test_multisegment_persists_thinking_segments(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    await _run_segmented(
        db_session, redis_client, billing_user, conversation,
        chunks=[
            StreamChunk(type="reasoning", delta="Анализирую вопросы. ",
                        segment_break=True, segment_index=0),
            StreamChunk(type="reasoning", delta="Детали анализа.",
                        segment_break=False, segment_index=0),
            StreamChunk(type="reasoning", delta="Готовлю ответ. ",
                        segment_break=True, segment_index=1),
            StreamChunk(type="reasoning", delta="Финальный разбор.",
                        segment_break=False, segment_index=1),
            StreamChunk(type="content", delta="Ответ."),
            StreamChunk(type="usage", input_tokens=10, output_tokens=5),
            StreamChunk(type="done"),
        ],
    )
    db_session.expunge_all()
    msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conversation.id,
                Message.role == "assistant",
            )
        )
    ).scalar_one()
    assert msg.thinking_segments == [
        {"id": "seg-0", "text": "Анализирую вопросы. Детали анализа.",
         "summary": "Анализирую вопросы"},
        {"id": "seg-1", "text": "Готовлю ответ. Финальный разбор.",
         "summary": "Готовлю ответ"},
    ]


async def test_single_segment_persists_null_segments(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    await _run_segmented(
        db_session, redis_client, billing_user, conversation,
        chunks=[
            StreamChunk(type="reasoning", delta="Думаю.",
                        segment_break=True, segment_index=0),
            StreamChunk(type="content", delta="Готово."),
            StreamChunk(type="usage", input_tokens=10, output_tokens=5),
            StreamChunk(type="done"),
        ],
    )
    db_session.expunge_all()
    msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conversation.id,
                Message.role == "assistant",
            )
        )
    ).scalar_one()
    # Single segment ⇒ legacy shape: NULL segments, falls back to `thinking`.
    assert msg.thinking_segments is None
    assert msg.thinking == "Думаю."


async def test_no_reasoning_emits_no_thinking_segment_events(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    """A stream with no reasoning chunks emits none of the thinking_* events
    (the thinking-off / no-reasoning case — acceptance tr-cy0.3.16 #3)."""
    events = await _run_segmented(
        db_session, redis_client, billing_user, conversation,
        chunks=[
            StreamChunk(type="content", delta="Просто ответ."),
            StreamChunk(type="usage", input_tokens=10, output_tokens=5),
            StreamChunk(type="done"),
        ],
    )
    names = [e.event for e in events]
    assert "thinking_segment" not in names
    assert "thinking_summary" not in names
    assert "thinking_delta" not in names
    # The assistant row carries no per-segment structure either.
    db_session.expunge_all()
    msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conversation.id,
                Message.role == "assistant",
            )
        )
    ).scalar_one()
    assert msg.thinking_segments is None
