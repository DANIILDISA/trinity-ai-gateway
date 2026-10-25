"""Mid-stream cancellation of process_chat_request (tr-99t).

When a client ABORTS a fast-streaming model (UI Stop / navigate-away), the
generator task is cancelled and asyncio.CancelledError — a BaseException — is
raised out of `await stream_iter.__anext__()`. It is caught by NONE of the
billing-pipeline's `except` blocks (there is no rollback/finalize there) and
there is no `finally`, so historically the reservation was left 'active'
(cron-swept later) and the partial reply was lost.

These tests drive process_chat_request as an async generator, let it collect a
couple of content chunks, then inject a CancelledError mid-stream and assert:
  - the partial assistant message was persisted (or, with no content yet, the
    reservation was released), AND
  - the reservation is no longer 'active', AND
  - the CancelledError is re-raised (never swallowed).
"""
import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import OpenRouterClientError
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.reservation import Reservation
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.api.v1.chat import get_conversation
from app.services.billing_service import process_chat_request
from app.services.billing_types import SSEEvent
from app.services.branching_service import (
    deactivate_active_tail_from,
    lock_conversation,
    repoint_active_leaf,
    resolve_active_path,
    set_active_leaf_to,
)
from app.services.message_service import save_user_message
from app.services.openrouter_proxy import StreamChunk

TEST_EMAIL = "stop_cancel@example.com"
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
    await session.execute(sa_delete(User).where(User.id == uid))
    await session.commit()


@pytest_asyncio.fixture(autouse=True)
async def _use_app_factory(app_session_factory):
    """Every test here drives process_chat_request and hits the abort cleanup,
    which opens its own session via get_session_factory() — initialize the app's
    global factory (shared conftest fixture) for the whole module."""
    yield


@pytest_asyncio.fixture(autouse=True)
async def _clear_rate_limit(redis_client: Redis):
    keys = await redis_client.keys("ratelimit:*")
    if keys:
        await redis_client.delete(*keys)
    yield
    keys = await redis_client.keys("ratelimit:*")
    if keys:
        await redis_client.delete(*keys)


@pytest_asyncio.fixture
async def billing_user(db_session: AsyncSession):
    await _purge_user(db_session, TEST_EMAIL)
    user = User(
        email=TEST_EMAIL,
        display_name="Стоп Тест",
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
    conv = Conversation(user_id=billing_user.id, model_id=MODEL_ID, title="Стоп")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    return conv


class _FakeProxy:
    """In-memory OpenRouterProxy stand-in for disconnect-path tests."""

    def __init__(
        self,
        chunks: list[StreamChunk] | None = None,
    ) -> None:
        self.chunks = chunks or []

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

        return gen()


async def _collect_events(gen: AsyncGenerator[SSEEvent, None]) -> list[SSEEvent]:
    out: list[SSEEvent] = []
    async for evt in gen:
        out.append(evt)
    return out


def _usage(input_tokens: int, output_tokens: int) -> StreamChunk:
    return StreamChunk(type="usage", input_tokens=input_tokens, output_tokens=output_tokens)


def _done() -> StreamChunk:
    return StreamChunk(type="done")


class _SlowProxy:
    """Yields content chunks with a small await between each so the stream is
    genuinely in-flight (suspended on an await) when the cancel hits — mirroring
    a fast-streaming model the user aborts mid-reply."""

    def __init__(self, deltas: list[str]) -> None:
        self.deltas = deltas

    def proxy_to_openrouter_with_retry(
        self, messages, model_id, max_retries=3, plugins=None, reasoning_budget=None
    ) -> AsyncGenerator[StreamChunk, None]:
        async def gen():
            for d in self.deltas:
                # Suspend so the cancelling task can interleave at this await.
                await asyncio.sleep(0)
                yield StreamChunk(type="content", delta=d)
            # Then idle long enough for the outer cancel to land mid-stream
            # (before any usage/done chunk arrives).
            await asyncio.sleep(30)
            yield StreamChunk(type="usage", input_tokens=10, output_tokens=3)
            yield StreamChunk(type="done")

        return gen()


async def test_cancel_mid_stream_persists_partial_and_clears_reservation(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    """Cancel a task driving process_chat_request after some content streamed:
    the partial assistant message is persisted, the reservation is finalized
    (no longer 'active'), and the CancelledError propagates."""
    user_id = billing_user.id
    conv_id = conversation.id
    user_msg = await save_user_message(db_session, conv_id, "Старт", parent_id=None)
    user_msg_id = user_msg.id
    await db_session.commit()

    started = asyncio.Event()

    async def _drive() -> None:
        async for evt in process_chat_request(
            user_id=user_id,
            conversation_id=conv_id,
            message_content="Старт",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=_SlowProxy(["часть 1 ", "часть 2 "]),
            assistant_parent_id=user_msg_id,
        ):
            if evt.event == "content_delta":
                # First content delta arrived — the stream is live; signal the
                # outer test it can cancel now.
                started.set()

    task = asyncio.ensure_future(_drive())
    # Wait until at least one content chunk has streamed (stream truly in-flight).
    await asyncio.wait_for(started.wait(), timeout=5)
    # Give the generator a tick to suspend on the long mid-stream sleep.
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    db_session.expire_all()
    reservation = (
        await db_session.execute(
            select(Reservation).where(Reservation.user_id == user_id)
        )
    ).scalar_one()
    # The reservation was cleaned up (finalized), NOT left 'active' for the cron.
    assert reservation.status != "active"
    assert reservation.status == "finalized"

    # The partial reply was persisted on the active branch.
    assistant = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conv_id, Message.role == "assistant"
            )
        )
    ).scalar_one()
    assert "часть 1" in assistant.content
    assert assistant.parent_id == user_msg_id
    assert assistant.is_active is True

    # A partial usage_log + debit landed; reserved is back to zero.
    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == reservation.request_id)
        )
    ).scalar_one()
    assert usage.status == "partial"
    assert usage.actual_cost_micro_rub > 0
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.reserved_micro_rub == 0
    assert user.balance_micro_rub < 10_000_000_000


async def test_cancel_before_any_content_releases_reservation(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    """Cancel before any content streamed: nothing to bill, so the reservation
    is RELEASED (not finalized) and no debit is taken."""
    user_id = billing_user.id
    conv_id = conversation.id
    await save_user_message(db_session, conv_id, "Старт", parent_id=None)
    await db_session.commit()

    stream_entered = asyncio.Event()

    class _StallProxy:
        def proxy_to_openrouter_with_retry(
            self, messages, model_id, max_retries=3, plugins=None, reasoning_budget=None
        ) -> AsyncGenerator[StreamChunk, None]:
            async def gen():
                # Entering the generator proves the pipeline passed STEP 4
                # (reservation created + committed) and is now awaiting the first
                # chunk. Signal the test, then stall so the cancel lands here
                # with NO content delivered.
                stream_entered.set()
                await asyncio.sleep(30)
                yield StreamChunk(type="done")

            return gen()

    async def _drive() -> None:
        async for _ in process_chat_request(
            user_id=user_id,
            conversation_id=conv_id,
            message_content="Старт",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=_StallProxy(),
        ):
            pass

    task = asyncio.ensure_future(_drive())
    # Wait until the stream generator is entered (reservation committed, awaiting
    # the first chunk) before cancelling.
    await asyncio.wait_for(stream_entered.wait(), timeout=5)
    await asyncio.sleep(0)  # let the generator suspend on its stall sleep
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    db_session.expire_all()
    reservation = (
        await db_session.execute(
            select(Reservation).where(Reservation.user_id == user_id)
        )
    ).scalar_one()
    assert reservation.status == "released"

    debits = (
        await db_session.execute(
            select(Transaction).where(
                Transaction.user_id == user_id, Transaction.type == "debit"
            )
        )
    ).scalars().all()
    assert debits == []

    # No assistant message persisted, reserved cleared, balance untouched.
    assistant = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conv_id, Message.role == "assistant"
            )
        )
    ).scalar_one_or_none()
    assert assistant is None
    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert user.reserved_micro_rub == 0
    assert user.balance_micro_rub == 10_000_000_000


async def test_cancel_cleanup_runs_on_separate_session(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for tr-04a: the abort cleanup must NOT reuse the request-scoped
    session.

    In production FastAPI tears down the request's `get_db` dependency
    (session.close -> connection.rollback) concurrently with this cleanup. Sharing
    one asyncpg connection across both raised
    `InterfaceError: another operation is in progress` and leaked the connection
    checked-out with an open transaction holding row locks, wedging every later
    request (incl. /auth/refresh -> 30s -> 503 -> perceived logout). The fix runs
    finalize/release on a fresh session from the factory. This test asserts the
    cleanup is handed a session that is NOT the request `db`, while preserving the
    billing outcome (reservation finalized).
    """
    import app.services.billing_service as bs

    real_cleanup = bs._finalize_or_release_on_abort
    captured: dict = {}

    async def _spy(cleanup_db: AsyncSession, redis: Redis, **kwargs):
        captured["session"] = cleanup_db
        return await real_cleanup(cleanup_db, redis, **kwargs)

    monkeypatch.setattr(bs, "_finalize_or_release_on_abort", _spy)

    user_id = billing_user.id
    conv_id = conversation.id
    user_msg = await save_user_message(db_session, conv_id, "Старт", parent_id=None)
    user_msg_id = user_msg.id
    await db_session.commit()

    started = asyncio.Event()

    async def _drive() -> None:
        async for evt in process_chat_request(
            user_id=user_id,
            conversation_id=conv_id,
            message_content="Старт",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=_SlowProxy(["часть 1 ", "часть 2 "]),
            assistant_parent_id=user_msg_id,
        ):
            if evt.event == "content_delta":
                started.set()

    task = asyncio.ensure_future(_drive())
    await asyncio.wait_for(started.wait(), timeout=5)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The cleanup ran — and on its OWN session, never the request-scoped one.
    assert "session" in captured, "abort cleanup was not invoked"
    assert captured["session"] is not db_session

    # Billing outcome preserved: the reservation was finalized on that separate
    # session (read back via a fresh transaction on the request session).
    db_session.expire_all()
    reservation = (
        await db_session.execute(
            select(Reservation).where(Reservation.user_id == user_id)
        )
    ).scalar_one()
    assert reservation.status == "finalized"


async def _answered_turn_then_regenerate_deactivation(
    db_session: AsyncSession, conv_id
) -> tuple:
    """Build an active 'user -> assistant' turn, then mirror the regenerate
    endpoint's pre-stream step: deactivate the prior answer + repoint the active
    leaf to the parent user message, committed before any new version exists.
    Returns (user_msg_id, prior_answer_id)."""
    user_msg = await save_user_message(db_session, conv_id, "Вопрос", parent_id=None)
    user_msg_id = user_msg.id
    prior = Message(
        conversation_id=conv_id,
        parent_id=user_msg_id,
        role="assistant",
        content="Старый ответ",
    )
    db_session.add(prior)
    await db_session.flush()
    prior_id = prior.id
    conv = await lock_conversation(db_session, conv_id)
    await set_active_leaf_to(db_session, conv, prior_id)
    await db_session.commit()

    locked = await lock_conversation(db_session, conv_id)
    prior_obj = await db_session.get(Message, prior_id)
    await deactivate_active_tail_from(db_session, conv_id, prior_obj)
    active = await resolve_active_path(db_session, conv_id)
    await repoint_active_leaf(db_session, locked, active[-1].id if active else None)
    await db_session.commit()

    # Sanity: the prior answer is now deactivated (the orphaning pre-stream state).
    db_session.expire_all()
    assert (await db_session.get(Message, prior_id)).is_active is False
    return user_msg_id, prior_id


async def _assert_prior_answer_restored(db_session: AsyncSession, conv_id, prior_id) -> None:
    """The prior answer is back on a coherent active path: content intact,
    is_active true, exactly one active assistant in the turn, and the conversation
    leaf resolves to it (no orphan)."""
    db_session.expire_all()
    prior_row = (
        await db_session.execute(select(Message).where(Message.id == prior_id))
    ).scalar_one()
    assert prior_row.content == "Старый ответ"
    assert prior_row.is_active is True

    active_assistants = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conv_id,
                Message.role == "assistant",
                Message.is_active.is_(True),
            )
        )
    ).scalars().all()
    assert len(active_assistants) == 1
    assert active_assistants[0].id == prior_id

    conv_after = (
        await db_session.execute(select(Conversation).where(Conversation.id == conv_id))
    ).scalar_one()
    leaf = await db_session.get(Message, conv_after.active_leaf_id)
    assert leaf is not None and leaf.role == "assistant" and leaf.id == prior_id


async def test_regenerate_then_cancel_before_content_restores_prior_answer(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    """tr-hvr1: regenerate an answered turn, then Stop BEFORE any content streams.

    The regenerate endpoint deactivates the prior answer and repoints the active
    leaf to the parent user message, committed before the new version exists. If
    the stream is aborted with no content, the empty-release path must RESTORE the
    prior answer to the active path — otherwise every sibling stays is_active=false
    and the answered turn renders as an empty slot on reload (visible data loss).
    """
    user_id = billing_user.id
    conv_id = conversation.id
    user_msg_id, prior_id = await _answered_turn_then_regenerate_deactivation(
        db_session, conv_id
    )

    # Regenerate streams but the client Stops before any content arrives.
    stream_entered = asyncio.Event()

    class _StallProxy:
        def proxy_to_openrouter_with_retry(
            self, messages, model_id, max_retries=3, plugins=None, reasoning_budget=None
        ) -> AsyncGenerator[StreamChunk, None]:
            async def gen():
                stream_entered.set()
                await asyncio.sleep(30)
                yield StreamChunk(type="done")

            return gen()

    async def _drive() -> None:
        async for _ in process_chat_request(
            user_id=user_id,
            conversation_id=conv_id,
            message_content="Вопрос",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=_StallProxy(),
            append_user_turn=False,
            assistant_parent_id=user_msg_id,
        ):
            pass

    task = asyncio.ensure_future(_drive())
    await asyncio.wait_for(stream_entered.wait(), timeout=5)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _assert_prior_answer_restored(db_session, conv_id, prior_id)


async def test_regenerate_error_before_content_restores_prior_answer(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    """tr-hvr1 (error path): a regenerate that fails with an OpenRouter error
    before any content (e.g. a paid model whose platform credits are exhausted →
    402) must ALSO restore the prior answer — the same orphan as the Stop case."""
    user_id = billing_user.id
    conv_id = conversation.id
    user_msg_id, prior_id = await _answered_turn_then_regenerate_deactivation(
        db_session, conv_id
    )

    class _ErrorProxy:
        def proxy_to_openrouter_with_retry(
            self, messages, model_id, max_retries=3, plugins=None, reasoning_budget=None
        ) -> AsyncGenerator[StreamChunk, None]:
            async def gen():
                raise OpenRouterClientError()
                yield StreamChunk(type="done")  # unreachable; makes this a generator

            return gen()

    events = await _collect_events(
        process_chat_request(
            user_id=user_id,
            conversation_id=conv_id,
            message_content="Вопрос",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=_ErrorProxy(),
            append_user_turn=False,
            assistant_parent_id=user_msg_id,
        )
    )
    assert any(e.event == "error" for e in events)

    await _assert_prior_answer_restored(db_session, conv_id, prior_id)


async def test_regenerate_non_newest_version_then_cancel_restores_that_version(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    """tr-hvr1 (review finding #1): if the user switched the < n/m > selector to a
    NON-newest version and regenerated it, a no-content abort must restore THAT
    version (the one that was on screen), not the newest sibling — otherwise the
    turn silently shows a different answer than the user had.
    """
    user_id = billing_user.id
    conv_id = conversation.id
    user_msg = await save_user_message(db_session, conv_id, "Вопрос", parent_id=None)
    user_msg_id = user_msg.id

    # Three answer versions of the one turn, with strictly increasing created_at so
    # V3 is unambiguously the newest sibling.
    base = datetime.now(timezone.utc)
    version_ids = []
    for i, text in enumerate(("Ответ V1", "Ответ V2", "Ответ V3")):
        m = Message(
            conversation_id=conv_id,
            parent_id=user_msg_id,
            role="assistant",
            content=text,
            created_at=base + timedelta(seconds=i),
        )
        db_session.add(m)
        await db_session.flush()
        version_ids.append(m.id)
    v1, v2, v3 = version_ids
    await db_session.commit()

    # The user switches the rendered version to V2 (NOT the newest).
    conv = await lock_conversation(db_session, conv_id)
    await set_active_leaf_to(db_session, conv, v2)
    await db_session.commit()

    # Mirror regenerate(target=V2): deactivate from V2 + repoint the leaf to user.
    locked = await lock_conversation(db_session, conv_id)
    v2_obj = await db_session.get(Message, v2)
    await deactivate_active_tail_from(db_session, conv_id, v2_obj)
    active = await resolve_active_path(db_session, conv_id)
    await repoint_active_leaf(db_session, locked, active[-1].id if active else None)
    await db_session.commit()

    stream_entered = asyncio.Event()

    class _StallProxy:
        def proxy_to_openrouter_with_retry(
            self, messages, model_id, max_retries=3, plugins=None, reasoning_budget=None
        ) -> AsyncGenerator[StreamChunk, None]:
            async def gen():
                stream_entered.set()
                await asyncio.sleep(30)
                yield StreamChunk(type="done")

            return gen()

    async def _drive() -> None:
        async for _ in process_chat_request(
            user_id=user_id,
            conversation_id=conv_id,
            message_content="Вопрос",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=_StallProxy(),
            append_user_turn=False,
            assistant_parent_id=user_msg_id,
            restore_leaf_on_failure_id=v2,  # the endpoint passes the target's id
        ):
            pass

    task = asyncio.ensure_future(_drive())
    await asyncio.wait_for(stream_entered.wait(), timeout=5)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # V2 (the on-screen version) is restored; V1 and the NEWER V3 stay inactive.
    db_session.expire_all()
    active_by_id = {
        m.id: m.is_active
        for m in (
            await db_session.execute(
                select(Message).where(
                    Message.conversation_id == conv_id, Message.role == "assistant"
                )
            )
        ).scalars().all()
    }
    assert active_by_id[v2] is True
    assert active_by_id[v1] is False
    assert active_by_id[v3] is False
    conv_after = (
        await db_session.execute(select(Conversation).where(Conversation.id == conv_id))
    ).scalar_one()
    assert conv_after.active_leaf_id == v2


async def test_partial_persists_openrouter_request_id(
    db_session: AsyncSession,
    redis_client: Redis,
    billing_user: User,
    conversation: Conversation,
) -> None:
    """On Stop, the gen-… id (seen on the content chunk before the abort) must be
    persisted on the partial usage_log so the cron can reconcile its cost."""
    conv_id = conversation.id
    calls = {"n": 0}

    async def is_disconnected() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # connected for the first poll, disconnected after

    proxy = _FakeProxy(chunks=[
        StreamChunk(type="content", delta="partial answer", openrouter_request_id="gen-part"),
        StreamChunk(type="content", delta=" more", openrouter_request_id="gen-part"),
        _usage(input_tokens=10, output_tokens=5),
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
        is_disconnected=is_disconnected,
    ))

    db_session.expire_all()
    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.conversation_id == conv_id)
        )
    ).scalar_one()
    assert usage.status == "partial"
    assert usage.openrouter_request_id == "gen-part"


async def test_get_conversation_heals_orphaned_regenerate_stop(
    db_session: AsyncSession,
    billing_user: User,
    conversation: Conversation,
) -> None:
    """tr-5o8r: a sub-~25ms regenerate→Stop can skip restore_orphaned_answer, leaving
    the active leaf on the parent user message with every answer version
    is_active=false. A plain GET of the conversation must persistently self-heal —
    restore the answer to the active path AND fix the DB (leaf back on the assistant),
    not just mask it on read. This drives the endpoint after the orphaning is committed.

    The orphan is built explicitly with a user message at an EARLIER created_at than
    its answer — the real regenerate shape, where the user turn was committed in a
    prior request — so the active user tail is stable (not the same-created_at
    over-deactivation a co-created test pair can hit)."""
    conv_id = conversation.id
    user_id = billing_user.id
    base = datetime(2026, 6, 8, tzinfo=timezone.utc)

    u = Message(
        conversation_id=conv_id, role="user", content="Вопрос",
        is_active=True, created_at=base,
    )
    db_session.add(u)
    await db_session.flush()
    a = Message(
        conversation_id=conv_id, parent_id=u.id, role="assistant",
        content="Старый ответ", model_id=MODEL_ID,
        is_active=False, created_at=base + timedelta(milliseconds=10),
    )
    db_session.add(a)
    await db_session.flush()
    a_id = a.id
    # Orphan: the leaf is stranded on the user message, the only answer deactivated.
    conv = await db_session.get(Conversation, conv_id)
    conv.active_leaf_id = u.id
    await db_session.commit()

    owner = await db_session.get(User, user_id)
    resp = await get_conversation(
        conversation_id=conv_id, include=None, user=owner, db=db_session
    )

    # The restored answer is the active tail (no empty slot in the response)...
    assert resp.messages[-1].role == "assistant"
    assert resp.messages[-1].content == "Старый ответ"
    # ...and the DB is now persistently coherent (leaf on the assistant, reactivated).
    db_session.expire_all()
    conv_after = await db_session.get(Conversation, conv_id)
    assert conv_after.active_leaf_id == a_id
    assert (await db_session.get(Message, a_id)).is_active is True
