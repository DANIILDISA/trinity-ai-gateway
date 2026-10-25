"""Branch-wiring on the linear send flow (spec §5, task 4-5)."""
import uuid
from collections.abc import AsyncGenerator

import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import Message
from app.models.reservation import Reservation
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services.billing_service import process_chat_request, save_messages
from app.services.billing_types import TokenUsage
from app.services.branching_service import (
    lock_conversation,
    repoint_active_leaf,
    resolve_active_path,
)
from app.services.message_service import save_user_message
from app.services.openrouter_proxy import StreamChunk

TEST_EMAIL = "branching_send@example.com"
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


@pytest_asyncio.fixture
async def branch_user(db_session: AsyncSession):
    await _purge_user(db_session, TEST_EMAIL)
    user = User(
        email=TEST_EMAIL,
        display_name="Ветка Тест",
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
async def conv(db_session: AsyncSession, branch_user: User) -> Conversation:
    c = Conversation(user_id=branch_user.id, model_id=MODEL_ID, title="Тест")
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


@pytest_asyncio.fixture(autouse=True)
async def _clear_rate_limit(redis_client: Redis):
    keys = await redis_client.keys("ratelimit:*")
    if keys:
        await redis_client.delete(*keys)
    yield


@pytest_asyncio.fixture(autouse=True)
async def _use_app_factory(app_session_factory):
    """Completed finalize runs on a FRESH get_session_factory() session (tr-sgz),
    so the global app factory must be initialized for these direct-call tests."""
    yield


class _FakeProxy:
    def __init__(self, chunks: list[StreamChunk]) -> None:
        self.chunks = chunks

    def proxy_to_openrouter_with_retry(
        self, messages, model_id, max_retries=3, plugins=None, reasoning_budget=None
    ) -> AsyncGenerator[StreamChunk, None]:
        async def gen():
            for chunk in self.chunks:
                yield chunk
        return gen()


def _turn() -> list[StreamChunk]:
    return [
        StreamChunk(type="content", delta="ответ"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=3),
        StreamChunk(type="done"),
    ]


async def _run_turn(db, redis, conv_id, user_text):
    """Simulate the send endpoint: save user msg off the active leaf, then bill."""
    leaf = (
        await db.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    await save_user_message(db, conv_id, user_text, parent_id=leaf)
    await db.commit()
    async for _ in process_chat_request(
        user_id=(await db.execute(select(Conversation.user_id).where(Conversation.id == conv_id))).scalar_one(),
        conversation_id=conv_id,
        message_content=user_text,
        model_id=MODEL_ID,
        db=db,
        redis=redis,
        proxy=_FakeProxy(_turn()),
    ):
        pass


async def test_user_message_parent_id_defaults_none_and_is_active(
    db_session: AsyncSession, branch_user: User, conv: Conversation
) -> None:
    msg = await save_user_message(db_session, conv.id, "Привет")
    await db_session.commit()
    msg_id = msg.id  # capture before expire_all() (avoids a lazy attr reload)
    db_session.expire_all()
    row = (
        await db_session.execute(select(Message).where(Message.id == msg_id))
    ).scalar_one()
    assert row.parent_id is None       # first turn → root
    assert row.is_active is True


async def test_user_message_parent_id_set_from_active_leaf(
    db_session: AsyncSession, branch_user: User, conv: Conversation
) -> None:
    prior = await save_user_message(db_session, conv.id, "родитель")
    await db_session.commit()
    prior_id = prior.id  # capture before expire_all() (avoids a lazy attr reload)
    child = await save_user_message(db_session, conv.id, "потомок", parent_id=prior_id)
    await db_session.commit()
    child_id = child.id
    db_session.expire_all()
    row = (
        await db_session.execute(select(Message).where(Message.id == child_id))
    ).scalar_one()
    assert row.parent_id == prior_id
    assert row.is_active is True


async def test_linear_two_turn_send_keeps_transcript_and_advances_leaf(
    db_session: AsyncSession, redis_client: Redis, branch_user: User, conv: Conversation
) -> None:
    conv_id = conv.id  # capture before expire_all() (avoids a lazy attr reload)
    await _run_turn(db_session, redis_client, conv_id, "первый вопрос")
    await _run_turn(db_session, redis_client, conv_id, "второй вопрос")

    db_session.expire_all()
    msgs = (
        await db_session.execute(
            select(Message)
            .where(Message.conversation_id == conv_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    ).scalars().all()
    # Same flat transcript a linear chat always produced.
    assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert [m.content for m in msgs] == ["первый вопрос", "ответ", "второй вопрос", "ответ"]
    # The second user message parents off the first assistant (the active leaf
    # at the time it was sent).
    u1, a1, u2, a2 = msgs
    assert u2.parent_id == a1.id
    # active_leaf_id advanced to the latest assistant (set in Task-5 STEP 7).
    leaf = (
        await db_session.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    assert leaf == a2.id


# ── Task 5: save_messages returns the assistant id; STEP 7 wires parent +
#    repoints active leaf under lock; message_done adds message_id+active_leaf_id;
#    append_user_turn parameter; partial-disconnect wiring (§0.1–§0.4). ───────────


async def _collect(gen) -> list:
    out = []
    async for e in gen:
        out.append(e)
    return out


class _CapturingProxy:
    """Records the context messages the orchestrator hands to the model so the
    append_user_turn test can assert the trailing user turn is/ isn't appended."""

    def __init__(self, chunks: list[StreamChunk]) -> None:
        self.chunks = chunks
        self.captured: list[list[dict]] = []

    def proxy_to_openrouter_with_retry(
        self, messages, model_id, max_retries=3, plugins=None, reasoning_budget=None
    ) -> AsyncGenerator[StreamChunk, None]:
        self.captured.append([dict(m) for m in messages])

        async def gen():
            for chunk in self.chunks:
                yield chunk

        return gen()


async def test_save_messages_returns_assistant_id(
    db_session: AsyncSession, branch_user: User, conv: Conversation
) -> None:
    conv_id = conv.id  # capture before expire_all() (avoids a lazy attr reload)
    rid = uuid.uuid4()
    returned = await save_messages(
        db_session,
        conversation_id=conv_id,
        assistant_content="ответ",
        actual_usage=TokenUsage(input_tokens=5, output_tokens=2, cost_usd=None),
        request_id=rid,
        model_id=MODEL_ID,
        cost_micro_rub=123,
    )
    await db_session.commit()
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conv_id, Message.role == "assistant"
            )
        )
    ).scalar_one()
    assert returned == row.id  # returns the new assistant id (was None)


async def test_save_messages_returns_none_for_ephemeral() -> None:
    # conversation_id=None → ephemeral, nothing persisted, returns None.
    out = await save_messages(
        None,  # type: ignore[arg-type]  # db unused on the early-return path
        conversation_id=None,
        assistant_content="x",
        actual_usage=TokenUsage(input_tokens=1, output_tokens=1, cost_usd=None),
        request_id=uuid.uuid4(),
        model_id=MODEL_ID,
        cost_micro_rub=0,
    )
    assert out is None


async def test_step7_wires_assistant_parent_and_repoints_leaf(
    db_session: AsyncSession, redis_client: Redis, branch_user: User, conv: Conversation
) -> None:
    conv_id = conv.id  # capture before expire_all() (avoids a lazy attr reload)
    # One turn through the real orchestrator flow.
    leaf0 = (
        await db_session.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    assert leaf0 is None
    user_msg = await save_user_message(db_session, conv_id, "вопрос", parent_id=leaf0)
    user_id = user_msg.id  # capture before expire_all()
    await db_session.commit()

    events = await _collect(
        process_chat_request(
            user_id=branch_user.id,
            conversation_id=conv_id,
            message_content="вопрос",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=_FakeProxy(_turn()),
            # send_message wires the assistant off the just-saved user node.
            assistant_parent_id=user_id,
        )
    )

    db_session.expire_all()
    assistant = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conv_id, Message.role == "assistant"
            )
        )
    ).scalar_one()
    # assistant parents off the user message it answered.
    assert assistant.parent_id == user_id
    # active_leaf repointed to the assistant.
    leaf = (
        await db_session.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    assert leaf == assistant.id

    # message_done SSE carries the real ids + leaf so the client can reconcile
    # both its placeholder bubbles (assistant AND the just-sent user message —
    # without user_message_id, editing/resending the user turn would 404).
    done = events[-1]
    assert done.event == "message_done"
    assert done.data["message_id"] == str(assistant.id)
    assert done.data["user_message_id"] == str(user_id)
    assert done.data["active_leaf_id"] == str(assistant.id)


async def test_active_path_invariant_after_send(
    db_session: AsyncSession, redis_client: Redis, branch_user: User, conv: Conversation
) -> None:
    """Walking is_active children from the root ends exactly at active_leaf_id,
    with exactly one active leaf (spec §3 invariant)."""
    conv_id = conv.id
    await _run_turn(db_session, redis_client, conv_id, "т1")
    await _run_turn(db_session, redis_client, conv_id, "т2")
    db_session.expire_all()

    rows = (
        await db_session.execute(
            select(Message)
            .where(Message.conversation_id == conv_id, Message.is_active.is_(True))
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    ).scalars().all()
    leaf = (
        await db_session.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    # Exactly one active leaf (no active row has an active child).
    active_ids = {m.id for m in rows}
    parents = {m.parent_id for m in rows if m.parent_id is not None}
    leaves = active_ids - parents
    assert leaves == {leaf}


async def test_append_user_turn_true_appends_trailing_user_turn(
    db_session: AsyncSession, redis_client: Redis, branch_user: User, conv: Conversation
) -> None:
    """Default (send) path: the new user turn IS appended to the model context."""
    conv_id = conv.id
    # Seed an active history ending in an assistant turn.
    await _run_turn(db_session, redis_client, conv_id, "история")

    # Save the new user turn off the active leaf, like the send endpoint does.
    leaf = (
        await db_session.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    await save_user_message(db_session, conv_id, "новый вопрос", parent_id=leaf)
    await db_session.commit()

    proxy = _CapturingProxy(_turn())
    await _collect(
        process_chat_request(
            user_id=branch_user.id,
            conversation_id=conv_id,
            message_content="новый вопрос",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=proxy,
            append_user_turn=True,
        )
    )
    ctx = proxy.captured[0]
    assert ctx[-1]["role"] == "user"
    assert ctx[-1]["content"] == "новый вопрос"


async def test_append_user_turn_false_skips_trailing_user_turn(
    db_session: AsyncSession, redis_client: Redis, branch_user: User, conv: Conversation
) -> None:
    """Regenerate/edit path (§0.3): the active history already ends at the user
    turn to answer, so no extra user turn is appended to context OR estimation."""
    conv_id = conv.id
    # Active history: a user turn then an assistant turn.
    await _run_turn(db_session, redis_client, conv_id, "история вопрос")

    proxy = _CapturingProxy(_turn())
    await _collect(
        process_chat_request(
            user_id=branch_user.id,
            conversation_id=conv_id,
            message_content="новый вопрос",  # must NOT be appended when False
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=proxy,
            append_user_turn=False,
        )
    )
    ctx = proxy.captured[0]
    # No appended trailing user turn — context is the active history verbatim,
    # which ends at the prior assistant message.
    assert ctx[-1]["role"] == "assistant"
    assert all(m["content"] != "новый вопрос" for m in ctx)


def _partial_turn() -> list[StreamChunk]:
    """A stream that delivers content but no usage/done — the disconnect fires
    mid-stream, so finalize_partial estimates from the collected text."""
    return [
        StreamChunk(type="content", delta="частичный "),
        StreamChunk(type="content", delta="ответ"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=3),
        StreamChunk(type="done"),
    ]


async def test_partial_disconnect_wires_parent_and_active_leaf(
    db_session: AsyncSession,
    redis_client: Redis,
    branch_user: User,
    conv: Conversation,
    app_session_factory,
) -> None:
    """§0.4: a mid-stream disconnect persists the partial assistant node ON the
    active branch — parent = the user turn it answered, active_leaf points at it."""
    conv_id = conv.id  # capture before expire_all()
    user_msg = await save_user_message(db_session, conv_id, "вопрос", parent_id=None)
    user_id = user_msg.id
    await db_session.commit()

    # Allow the first inter-chunk check through (collect content), then disconnect.
    calls = {"n": 0}

    async def _disc() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2

    await _collect(
        process_chat_request(
            user_id=branch_user.id,
            conversation_id=conv_id,
            message_content="вопрос",
            model_id=MODEL_ID,
            db=db_session,
            redis=redis_client,
            proxy=_FakeProxy(_partial_turn()),
            is_disconnected=_disc,
        )
    )

    db_session.expire_all()
    assistant = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conv_id, Message.role == "assistant"
            )
        )
    ).scalar_one()
    # Partial node landed on the active branch off the user turn.
    assert assistant.parent_id == user_id
    assert assistant.is_active is True
    assert "частичный" in assistant.content
    leaf = (
        await db_session.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    assert leaf == assistant.id


# ── §0.1 user-node-creation critical section (concurrency fix) ───────────────
#
# The send endpoint takes the conversation row lock (SELECT … FOR UPDATE),
# derives the parent from the LOCKED active path, saves the user node, and
# advances active_leaf_id to it — all in the short transaction that commits the
# user message BEFORE streaming. These tests exercise exactly that section.


async def _save_user_node_under_lock(
    db: AsyncSession, conv_id: uuid.UUID, content: str
) -> Message:
    """Mirror the endpoint's user-save critical section (chat.send_message):
    lock the conversation, derive the parent from the locked active path, save
    the user node, advance the active leaf to it, then commit (release lock)."""
    conv = await lock_conversation(db, conv_id)
    active = await resolve_active_path(db, conv_id)
    parent_id = active[-1].id if active else None
    msg = await save_user_message(db, conv_id, content, parent_id=parent_id)
    await repoint_active_leaf(db, conv, msg.id)
    await db.commit()
    return msg


async def _assert_single_active_leaf(db: AsyncSession, conv_id: uuid.UUID) -> uuid.UUID:
    """The §8 invariant: exactly one active leaf, and it equals active_leaf_id."""
    rows = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conv_id, Message.is_active.is_(True))
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    ).scalars().all()
    leaf = (
        await db.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    active_ids = {m.id for m in rows}
    parents = {m.parent_id for m in rows if m.parent_id is not None}
    leaves = active_ids - parents
    assert leaves == {leaf}, f"expected single active leaf {leaf}, got {leaves}"
    return leaf


async def test_user_save_critical_section_advances_leaf_to_user_node(
    db_session: AsyncSession, branch_user: User, conv: Conversation
) -> None:
    """After the user-save critical section, active_leaf_id points at the NEW
    user node, and that node's parent_id is the prior active leaf."""
    conv_id = conv.id  # capture before expire_all()
    # First turn: prior leaf is None → user node is the root.
    u1 = await _save_user_node_under_lock(db_session, conv_id, "первый")
    u1_id = u1.id
    db_session.expire_all()
    row1 = (
        await db_session.execute(select(Message).where(Message.id == u1_id))
    ).scalar_one()
    leaf1 = (
        await db_session.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    assert row1.parent_id is None
    assert row1.is_active is True
    assert leaf1 == u1_id  # leaf advanced to the user node

    # Second turn under lock: parents off the prior leaf (u1), advances leaf.
    u2 = await _save_user_node_under_lock(db_session, conv_id, "второй")
    u2_id = u2.id
    db_session.expire_all()
    row2 = (
        await db_session.execute(select(Message).where(Message.id == u2_id))
    ).scalar_one()
    leaf2 = (
        await db_session.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    assert row2.parent_id == u1_id  # parents off the prior locked leaf
    assert leaf2 == u2_id
    await _assert_single_active_leaf(db_session, conv_id)


async def test_two_sequential_user_saves_under_lock_keep_single_active_leaf(
    db_session: AsyncSession, branch_user: User, conv: Conversation
) -> None:
    """Sequential-equivalent of the race: two user saves run the NEW way (each
    under the conversation lock, deriving the parent from the locked active
    path). The second chains off the first → still exactly one active leaf.

    Contrast with the OLD way (both reading the same stale conv.active_leaf_id),
    which is asserted to produce TWO active leaves below."""
    conv_id = conv.id
    u1 = await _save_user_node_under_lock(db_session, conv_id, "A")
    u1_id = u1.id
    u2 = await _save_user_node_under_lock(db_session, conv_id, "B")
    u2_id = u2.id
    db_session.expire_all()
    leaf = await _assert_single_active_leaf(db_session, conv_id)
    assert leaf == u2_id
    row2 = (
        await db_session.execute(select(Message).where(Message.id == u2_id))
    ).scalar_one()
    assert row2.parent_id == u1_id


async def test_old_way_two_user_saves_off_stale_leaf_forks_two_leaves(
    db_session: AsyncSession, branch_user: User, conv: Conversation
) -> None:
    """Characterization of the DEFECT the fix removes: when both user saves
    parent off the SAME (stale) active_leaf_id and neither advances the leaf,
    the active set has TWO leaves — a non-linear active path."""
    conv_id = conv.id
    stale_leaf = (
        await db_session.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()  # None on a fresh conversation
    await save_user_message(db_session, conv_id, "race-A", parent_id=stale_leaf)
    await save_user_message(db_session, conv_id, "race-B", parent_id=stale_leaf)
    await db_session.commit()
    db_session.expire_all()

    rows = (
        await db_session.execute(
            select(Message)
            .where(Message.conversation_id == conv_id, Message.is_active.is_(True))
        )
    ).scalars().all()
    active_ids = {m.id for m in rows}
    parents = {m.parent_id for m in rows if m.parent_id is not None}
    leaves = active_ids - parents
    assert len(leaves) == 2  # the broken state: two active leaves


# NOTE on a "true" two-connection FOR UPDATE race test:
# A genuine race (two independent sessions, one holding the conversation lock with
# a deliberate sleep while the other contends) was prototyped and confirms the fix:
# under the lock the second send blocks, then chains off the first → one active
# leaf, whereas the OLD no-lock path forks two active leaves. It is intentionally
# NOT kept in the suite: holding a FOR UPDATE lock across an await on a pooled
# connection means any abnormal teardown (a cancelled gather / killed worker)
# leaves an `idle in transaction` backend that holds the conversation-row lock and
# stalls every subsequent test (and `clean_database`) for the backend's lifetime —
# a suite-wide footgun for a deterministic CI gate. Per the task's own fallback
# guidance, the serialized invariant is asserted via the sequential-equivalent
# (`test_two_sequential_user_saves_under_lock_keep_single_active_leaf`) plus the
# OLD-way defect characterization (`test_old_way_two_user_saves_off_stale_leaf_
# forks_two_leaves`) and the real-endpoint test in test_chat_sse.py.
