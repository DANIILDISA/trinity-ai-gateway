"""Hard-Stop persistence for the agentic tool loop (tr-tc0).

A Stop mid agentic-loop must persist tool_steps, charge only the completed rounds
(operator-favor, no leak), and keep the created artifact downloadable after the
request session is rolled back. Drives process_chat_request to a REAL CancelledError
and asserts the A+B+C2 behaviour.
"""
import asyncio
import time

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import delete as sa_delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.artifact import Artifact
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.model_pricing import ModelPricing
from app.models.reservation import Reservation
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services.billing_service import process_chat_request
from app.services.message_service import save_user_message
from app.services.openrouter_proxy import StreamChunk
from app.services.tools.file_tools import register_builtin_tools

pytestmark = pytest.mark.asyncio
MODEL_ID = "openai/gpt-4.1-mini"
TEST_EMAIL = "tooluse_stop@example.com"
INITIAL_BALANCE = 10_000_000_000


def _tc(index, name=None, args=None, id=None):
    return StreamChunk(
        type="tool_call", tool_call_index=index, tool_call_id=id,
        name=name, arguments_delta=args,
    )


async def _purge(session: AsyncSession, email: str) -> None:
    u = (
        await session.execute(select(User).where(User.email_lower == email.lower()))
    ).scalar_one_or_none()
    if u is None:
        return
    uid = u.id
    convs = select(Conversation.id).where(Conversation.user_id == uid)
    await session.execute(sa_delete(Artifact).where(Artifact.user_id == uid))
    await session.execute(sa_delete(Message).where(Message.conversation_id.in_(convs)))
    await session.execute(sa_delete(Conversation).where(Conversation.user_id == uid))
    await session.execute(sa_delete(UsageLog).where(UsageLog.user_id == uid))
    await session.execute(sa_delete(Reservation).where(Reservation.user_id == uid))
    await session.execute(sa_delete(Transaction).where(Transaction.user_id == uid))
    await session.execute(sa_delete(User).where(User.id == uid))
    await session.commit()


@pytest.fixture(autouse=True)
def _tools_registered():
    register_builtin_tools()


@pytest_asyncio.fixture(autouse=True)
async def _use_app_factory(app_session_factory):
    # The abort cleanup opens its OWN session via get_session_factory(); the shared
    # conftest fixture initializes the app's global factory for this module.
    yield


@pytest_asyncio.fixture(autouse=True)
async def _clear_rate_limit(redis_client: Redis):
    for k in (await redis_client.keys("ratelimit:*")) or []:
        await redis_client.delete(k)
    yield


@pytest_asyncio.fixture
async def user(db_session: AsyncSession) -> User:
    await _purge(db_session, TEST_EMAIL)
    u = User(
        email=TEST_EMAIL, display_name="Стоп Тул", password_hash=None, role="user",
        status="active", balance_micro_rub=INITIAL_BALANCE, reserved_micro_rub=0,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    yield u
    db_session.expire_all()
    await _purge(db_session, TEST_EMAIL)


@pytest_asyncio.fixture
async def conversation(db_session: AsyncSession, user: User) -> Conversation:
    conv = Conversation(user_id=user.id, model_id=MODEL_ID, title="Стоп")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    return conv


async def _set_tools(db: AsyncSession, value: bool) -> None:
    await db.execute(
        update(ModelPricing)
        .where(ModelPricing.model_id == MODEL_ID, ModelPricing.is_current.is_(True))
        .values(supports_tools=value)
    )
    await db.commit()


CREATE_BIG = [
    _tc(0, name="create_file", id="c1", args='{"path":"big.md","file_text":"l1\\nl2\\nl3"}'),
    StreamChunk(type="usage", input_tokens=20, output_tokens=10),
    StreamChunk(type="done"),
]


class _Round0ThenStallProxy:
    """Round 0 = the given chunks (a completed tool round). Round 1 stalls before
    any answer text (optionally after one content delta), so a cancel during the
    stall lands with a COMPLETED tool round."""

    def __init__(self, round0=CREATE_BIG, content_first: str | None = None):
        self.round0 = round0
        self.content_first = content_first
        self.calls = 0
        # Set when round 1 begins its stall — i.e. round 0 (all its tool calls) has
        # FULLY executed. The harness cancels on this, so a multi-tool round isn't
        # cut off after its first tool_result.
        self.stalled = asyncio.Event()

    def proxy_to_openrouter_with_retry(
        self, messages, model_id, max_retries=3, plugins=None,
        reasoning_budget=None, tools=None,
    ):
        self.calls += 1
        first = self.calls == 1
        round0, content_first, stalled = self.round0, self.content_first, self.stalled

        async def gen():
            if first:
                for c in round0:
                    yield c
            else:
                if content_first is not None:
                    yield StreamChunk(type="content", delta=content_first)
                stalled.set()
                await asyncio.sleep(30)  # stall so the outer cancel lands here
                yield StreamChunk(type="content", delta="никогда")
                yield StreamChunk(type="done")

        return gen()


async def _run_until_then_cancel(
    proxy, db, redis, user_id, conv_id, parent_id, *, collect=None
):
    """Drive process_chat_request until the proxy enters its round-1 stall (round 0
    fully executed; any `content_first` already streamed), then cancel mid-stream
    (REAL Stop). Robust to multi-tool rounds — unlike triggering on an SSE event."""

    async def _drive():
        async for evt in process_chat_request(
            user_id=user_id, conversation_id=conv_id,
            message_content="сделай big.md", model_id=MODEL_ID,
            db=db, redis=redis, proxy=proxy,
            assistant_parent_id=parent_id, tools_enabled=True,
        ):
            if collect is not None:
                collect.append(evt)

    task = asyncio.ensure_future(_drive())
    await asyncio.wait_for(proxy.stalled.wait(), timeout=10)
    await asyncio.sleep(0)  # ensure the generator is suspended in the stall
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_stop_during_answer_persists_tool_steps(
    db_session: AsyncSession, redis_client: Redis, user: User, conversation: Conversation
):
    """Stop after a completed tool round, mid answer text: the assistant message
    persists WITH the tool timeline, and no step is left `running` (B)."""
    await _set_tools(db_session, True)
    conv_id = conversation.id
    um = await save_user_message(db_session, conv_id, "сделай big.md", parent_id=None)
    await db_session.commit()

    proxy = _Round0ThenStallProxy(content_first="Готов")
    await _run_until_then_cancel(
        proxy, db_session, redis_client, user.id, conv_id, um.id
    )

    db_session.expire_all()
    msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conv_id, Message.role == "assistant"
            )
        )
    ).scalar_one()
    assert "Готов" in msg.content
    assert msg.tool_steps, "tool_steps must persist on a tool-mode Stop"
    assert msg.tool_steps[0]["tool"] == "create_file"
    assert all(s["status"] != "running" for s in msg.tool_steps)  # coerced


async def test_stop_before_answer_persists_and_charges_completed_rounds(
    db_session: AsyncSession, redis_client: Redis, user: User, conversation: Conversation
):
    """Stop after a completed tool round but BEFORE any answer text: the message is
    PERSISTED (not released) and the user is charged for the completed round —
    closing the money-leak. In-flight (never-reported) work stays uncharged (A)."""
    await _set_tools(db_session, True)
    conv_id = conversation.id
    # The abort cleanup rolls back the request session (= db_session here), which
    # expires fixture ORM objects — capture ids as locals (mirrors test_chat_stop_cancel).
    user_id = user.id
    um = await save_user_message(db_session, conv_id, "сделай big.md", parent_id=None)
    await db_session.commit()

    proxy = _Round0ThenStallProxy()  # round 1 stalls with NO content
    await _run_until_then_cancel(
        proxy, db_session, redis_client, user_id, conv_id, um.id
    )

    db_session.expire_all()
    res = (
        await db_session.execute(select(Reservation).where(Reservation.user_id == user_id))
    ).scalar_one()
    assert res.status == "finalized"  # NOT "released" — no full refund / no leak
    usage = (
        await db_session.execute(select(UsageLog).where(UsageLog.request_id == res.request_id))
    ).scalar_one()
    assert usage.status == "partial"
    assert usage.actual_cost_micro_rub > 0          # completed round 0 (20/10) charged
    u = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert u.reserved_micro_rub == 0
    assert u.balance_micro_rub < INITIAL_BALANCE    # debit happened
    msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conv_id, Message.role == "assistant"
            )
        )
    ).scalar_one()
    assert msg.content == ""                          # empty bubble; timeline carries it
    assert msg.tool_steps and msg.tool_steps[0]["status"] == "done"


async def test_shield_commits_artifact_when_cancelled_mid_executor(
    db_session, redis_client, user, conversation, monkeypatch
):
    """tr-ans headline robustness: a CancelledError delivered WHILE a tool executor
    holds a flushed-but-uncommitted artifact must NOT lose it. The engine's
    anyio.CancelScope(shield=True) defers the cancel until after the commit, so the
    artifact is committed and the abort path links it by id — exactly one row,
    linked to the partial message.

    Cancellation is delivered through an anyio cancel scope (tg.cancel_scope.cancel()),
    mirroring production exactly: under uvicorn/Starlette a client Stop/disconnect
    cancels via anyio's structured cancellation, NOT a raw asyncio Task.cancel(). The
    engine's anyio shield only defers a cancel that flows through anyio's scope stack
    (a raw Task.cancel() on a plain asyncio loop is delivered at the next await before
    the shield can intercept it) — so the test must cancel the way the runtime does to
    exercise the real shielded-commit path."""
    import asyncio
    import uuid as _uuid

    import anyio

    import app.services.tools.file_tools as ft
    from app.schemas.tools import ToolResultPayload
    from app.services.tools import registry as _registry

    await _set_tools(db_session, True)
    conv_id = conversation.id
    user_id = user.id
    um = await save_user_message(db_session, conv_id, "сделай s.md", parent_id=None)
    await db_session.commit()

    flushed = asyncio.Event()

    async def _slow_create(args, ctx):
        art = ft.Artifact(
            user_id=ctx.user_id, conversation_id=ctx.conversation_id,
            group_id=_uuid.uuid4(), version=1, filename="s.md",
            media_type="text/plain", content="hello", size_bytes=5,
        )
        ctx.db.add(art)
        await ctx.db.flush()
        ctx.created_artifact_ids.append(art.id)
        flushed.set()
        await asyncio.sleep(0.2)  # cancel is requested here
        return ToolResultPayload(status="ok", filename="s.md", output=" готово")

    real = _registry.get("create_file")
    slow_spec = _registry.ToolSpec(
        name="create_file", definition=real.definition,
        executor=_slow_create, safety_profile=real.safety_profile,
    )
    monkeypatch.setitem(_registry._registry, "create_file", slow_spec)
    # process_chat_request calls register_builtin_tools() at the top of its tool path,
    # which would re-register the real create_file and clobber the swap above — no-op
    # it so the slow executor survives into the engine round.
    monkeypatch.setattr(ft, "register_builtin_tools", lambda: None)

    # Round 0 issues the (slow) create_file; round 1 stalls so the deferred cancel
    # lands while the loop is still in-flight — the ABORT path (not the completed
    # path) is what must link the engine-committed artifact.
    proxy = _Round0ThenStallProxy(round0=[
        _tc(0, name="create_file", id="c1", args='{"path":"s.md","file_text":"hello"}'),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])

    async def _drive():
        async for _ in process_chat_request(
            user_id=user_id, conversation_id=conv_id, message_content="сделай s.md",
            model_id=MODEL_ID, db=db_session, redis=redis_client, proxy=proxy,
            assistant_parent_id=um.id, tools_enabled=True,
        ):
            pass

    # Drive process_chat_request inside an anyio task group and cancel through its
    # cancel scope once the executor has flushed (the same structured cancellation
    # Starlette delivers on a client Stop). The scope swallows the cancel at the group
    # boundary, so no CancelledError surfaces here.
    async def _cancel_when_flushed(scope):
        await flushed.wait()
        scope.cancel()

    with anyio.move_on_after(15):
        async with anyio.create_task_group() as tg:
            tg.start_soon(_cancel_when_flushed, tg.cancel_scope)
            tg.start_soon(_drive)

    db_session.expire_all()
    arts = (await db_session.execute(
        select(Artifact).where(Artifact.conversation_id == conv_id, Artifact.filename == "s.md")
    )).scalars().all()
    assert len(arts) == 1, "shield must commit exactly one artifact, no loss/duplicate"
    msg = (await db_session.execute(
        select(Message).where(Message.conversation_id == conv_id, Message.role == "assistant")
    )).scalar_one()
    assert arts[0].message_id == msg.id, "committed artifact must be linked by id to the partial message"


class _RoundProxy:
    """Replays a fixed list of rounds (normal completion, no abort)."""

    def __init__(self, rounds):
        self.rounds = rounds
        self.calls = 0

    def proxy_to_openrouter_with_retry(
        self, messages, model_id, max_retries=3, plugins=None,
        reasoning_budget=None, tools=None,
    ):
        chunks = self.rounds[min(self.calls, len(self.rounds) - 1)]
        self.calls += 1

        async def gen():
            for c in chunks:
                yield c

        return gen()


async def test_normal_completion_charge_unchanged_by_inloop_usage_build(
    db_session: AsyncSession, redis_client: Redis, user: User, conversation: Conversation
):
    """Adding the in-loop actual_usage build must NOT change normal-completion
    billing: a clean 2-round run still charges off the summed actual usage (50/18)."""
    await _set_tools(db_session, True)
    conv_id = conversation.id
    um = await save_user_message(db_session, conv_id, "сделай a.md", parent_id=None)
    await db_session.commit()

    proxy = _RoundProxy([CREATE_BIG, [
        StreamChunk(type="content", delta="Готово!"),
        StreamChunk(type="usage", input_tokens=30, output_tokens=8),
        StreamChunk(type="done"),
    ]])
    events = [e async for e in process_chat_request(
        user_id=user.id, conversation_id=conv_id, message_content="сделай a.md",
        model_id=MODEL_ID, db=db_session, redis=redis_client, proxy=proxy,
        assistant_parent_id=um.id, tools_enabled=True,
    )]
    done = events[-1]
    assert done.event == "message_done"
    assert done.data["tokens_used"]["input"] == 50    # 20 + 30, summed actuals
    assert done.data["tokens_used"]["output"] == 18   # 10 + 8


async def test_stop_links_committed_artifact_by_id_no_duplicate(
    db_session: AsyncSession, redis_client: Redis, user: User, conversation: Conversation
):
    """The artifact created in the aborted turn was COMMITTED live by the engine, so
    the abort path links it by id to the persisted partial message — exactly ONE row
    (no re-persist duplicate), and the download endpoint resolves (tr-ans, replaces
    the tr-tc0 C2 re-persist)."""
    await _set_tools(db_session, True)
    conv_id = conversation.id
    user_id = user.id
    um = await save_user_message(db_session, conv_id, "сделай big.md", parent_id=None)
    await db_session.commit()

    proxy = _Round0ThenStallProxy()  # creates big.md = "l1\nl2\nl3", then stalls
    await _run_until_then_cancel(proxy, db_session, redis_client, user_id, conv_id, um.id)

    db_session.expire_all()
    arts = (await db_session.execute(
        select(Artifact)
        .where(Artifact.conversation_id == conv_id, Artifact.filename == "big.md")
        .order_by(Artifact.version.desc())
    )).scalars().all()
    assert len(arts) == 1
    assert arts[0].content == "l1\nl2\nl3"
    assert arts[0].message_id is not None


MULTI_VERSION_ROUND = [
    _tc(0, name="create_file", id="a0", args='{"path":"p.md","file_text":"l1\\nl2"}'),
    _tc(1, name="str_replace", id="a1", args='{"path":"p.md","old_str":"l1","new_str":"L1"}'),
    _tc(2, name="str_replace", id="a2", args='{"path":"p.md","old_str":"l2","new_str":"L2"}'),
    StreamChunk(type="usage", input_tokens=10, output_tokens=5),
    StreamChunk(type="done"),
]
ANSWER_ROUND = [
    StreamChunk(type="content", delta="ok"),
    StreamChunk(type="usage", input_tokens=1, output_tokens=1),
    StreamChunk(type="done"),
]


async def test_stop_prune_parity_matches_normal_completion(
    db_session: AsyncSession, redis_client: Redis, user: User,
    conversation: Conversation, monkeypatch,
):
    """The cleanup-session prune must keep exactly the versions a normal completion
    would (tr-tc0 / cause D prune-parity). Cap lowered to 2 so a 3-version turn prunes."""
    import app.services.tools.file_tools as ft
    monkeypatch.setattr(ft, "MAX_VERSIONS_PER_GROUP", 2)
    await _set_tools(db_session, True)
    # Capture ids upfront — expire_all()/abort-rollback below expires fixture objects.
    user_id = user.id
    conv_id = conversation.id

    # Control: a NORMAL completion on its own conversation (live prune to cap).
    ctrl = Conversation(user_id=user_id, model_id=MODEL_ID, title="ctrl")
    db_session.add(ctrl)
    await db_session.commit()
    await db_session.refresh(ctrl)
    ctrl_id = ctrl.id
    um_c = await save_user_message(db_session, ctrl_id, "p", parent_id=None)
    await db_session.commit()
    async for _ in process_chat_request(
        user_id=user_id, conversation_id=ctrl_id, message_content="p", model_id=MODEL_ID,
        db=db_session, redis=redis_client, proxy=_RoundProxy([MULTI_VERSION_ROUND, ANSWER_ROUND]),
        assistant_parent_id=um_c.id, tools_enabled=True,
    ):
        pass
    db_session.expire_all()
    ctrl_versions = sorted(
        v for (v,) in (await db_session.execute(
            select(Artifact.version).where(Artifact.conversation_id == ctrl_id)
        )).all()
    )

    # Abort: same 3-version round, then Stop before the answer.
    um_a = await save_user_message(db_session, conv_id, "p", parent_id=None)
    await db_session.commit()
    await _run_until_then_cancel(
        _Round0ThenStallProxy(round0=MULTI_VERSION_ROUND), db_session, redis_client,
        user_id, conv_id, um_a.id,
    )
    db_session.expire_all()
    abort_versions = sorted(
        v for (v,) in (await db_session.execute(
            select(Artifact.version).where(Artifact.conversation_id == conv_id)
        )).all()
    )

    assert abort_versions == ctrl_versions == [2, 3]  # same survivors, pruned to cap


async def test_abort_persist_does_not_deadlock_on_conversation_lock(
    db_session: AsyncSession, redis_client: Redis, user: User, conversation: Conversation
):
    """Regression for cause D (tr-tc0), HEALTHY-connection variant. Round 0 flushes
    an artifact (the request session holds a FOR KEY SHARE lock on the conversation
    row); the abort-persist cleanup needs FOR UPDATE on that same row. On the pre-fix
    code these conflict and the cleanup hangs ~60s until idle_in_transaction kills the
    request connection. The rollback-first fix releases the lock, so this completes in
    well under the timeout — assert it does, so a regression of cause D is caught."""
    await _set_tools(db_session, True)
    conv_id = conversation.id
    user_id = user.id
    um = await save_user_message(db_session, conv_id, "сделай big.md", parent_id=None)
    await db_session.commit()

    start = time.perf_counter()
    await _run_until_then_cancel(
        _Round0ThenStallProxy(), db_session, redis_client, user_id, conv_id, um.id,
    )
    elapsed = time.perf_counter() - start
    # Normal completion is ~1-3s; the deadlock is ~60s. 30s cleanly separates them.
    assert elapsed < 30, (
        f"abort-persist took {elapsed:.1f}s — conversation-lock deadlock regressed (cause D)"
    )
    # And it genuinely finalized (didn't just bail early).
    db_session.expire_all()
    res = (
        await db_session.execute(select(Reservation).where(Reservation.user_id == user_id))
    ).scalar_one()
    assert res.status == "finalized"


async def test_abort_persist_tolerates_dead_request_connection(
    db_session: AsyncSession, redis_client: Redis, user: User,
    conversation: Conversation, monkeypatch,
):
    """Cause D, ALREADY-KILLED-connection variant. If the request connection was
    already terminated (idle_in_transaction / admin kill), its locks are gone but
    request_db.rollback() raises — the cleanup must swallow it and still finalize on
    the fresh session. A no-flush tool round (view_file on a missing file) means no
    real conversation lock is held, isolating rollback-failure tolerance."""
    import sqlalchemy.exc as sa_exc

    await _set_tools(db_session, True)
    conv_id = conversation.id
    user_id = user.id
    um = await save_user_message(db_session, conv_id, "посмотри ghost.md", parent_id=None)
    await db_session.commit()

    async def _boom():
        raise sa_exc.InterfaceError("connection is closed", None, Exception())

    monkeypatch.setattr(db_session, "rollback", _boom)

    view_missing_round = [
        _tc(0, name="view_file", id="v1", args='{"path":"ghost.md"}'),
        StreamChunk(type="usage", input_tokens=15, output_tokens=4),
        StreamChunk(type="done"),
    ]
    await _run_until_then_cancel(
        _Round0ThenStallProxy(round0=view_missing_round), db_session, redis_client,
        user_id, conv_id, um.id,
    )
    monkeypatch.undo()  # restore real rollback for assertions / teardown

    db_session.expire_all()
    res = (
        await db_session.execute(select(Reservation).where(Reservation.user_id == user_id))
    ).scalar_one()
    assert res.status == "finalized"  # finalize ran despite the rollback failure
    msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conv_id, Message.role == "assistant"
            )
        )
    ).scalar_one()
    assert msg.tool_steps and msg.tool_steps[0]["tool"] == "view_file"


async def test_stop_with_nothing_delivered_releases(
    db_session: AsyncSession, redis_client: Redis, user: User, conversation: Conversation
):
    """Tool mode, but Stop BEFORE any tool step or content (round 0 stalls): no
    tool_steps, no content → the gate (collected_content OR tool_steps) is false, so
    the reservation is RELEASED — no spurious persist/charge (the negative control)."""
    await _set_tools(db_session, True)
    conv_id = conversation.id
    user_id = user.id
    await save_user_message(db_session, conv_id, "подожди", parent_id=None)
    await db_session.commit()

    stalled = asyncio.Event()

    class _StallRound0:
        def proxy_to_openrouter_with_retry(
            self, messages, model_id, max_retries=3, plugins=None,
            reasoning_budget=None, tools=None,
        ):
            async def gen():
                stalled.set()
                await asyncio.sleep(30)  # stall before any tool_call / content
                yield StreamChunk(type="done")

            return gen()

    async def _drive():
        async for _ in process_chat_request(
            user_id=user_id, conversation_id=conv_id, message_content="подожди",
            model_id=MODEL_ID, db=db_session, redis=redis_client,
            proxy=_StallRound0(), tools_enabled=True,
        ):
            pass

    task = asyncio.ensure_future(_drive())
    await asyncio.wait_for(stalled.wait(), timeout=10)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    db_session.expire_all()
    res = (
        await db_session.execute(select(Reservation).where(Reservation.user_id == user_id))
    ).scalar_one()
    assert res.status == "released"
    debits = (
        await db_session.execute(
            select(Transaction).where(
                Transaction.user_id == user_id, Transaction.type == "debit"
            )
        )
    ).scalars().all()
    assert debits == []
    msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conv_id, Message.role == "assistant"
            )
        )
    ).scalar_one_or_none()
    assert msg is None
    u = (await db_session.execute(select(User).where(User.id == user_id))).scalar_one()
    assert u.reserved_micro_rub == 0
    assert u.balance_micro_rub == INITIAL_BALANCE   # nothing charged
