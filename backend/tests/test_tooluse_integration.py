"""End-to-end agentic tool loop through process_chat_request (tr-cy0.2.5).

A multi-round fake proxy drives the engine via the orchestrator: the model emits
a create_file tool call, Trinity executes it, feeds the result back, the model
answers. Asserts the live SSE sequence, summed billing, persisted tool_steps +
artifact, and the gate (tools off / unsupported model → single-shot path).
"""
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.artifact import Artifact
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.model_pricing import ModelPricing
from app.models.user import User
from app.services.billing_service import process_chat_request
from app.services.billing_types import SSEEvent
from app.services.openrouter_proxy import StreamChunk
from app.services.tools.file_tools import register_builtin_tools

pytestmark = pytest.mark.asyncio
MODEL_ID = "openai/gpt-4.1-mini"
TEST_EMAIL = "tooluse_integration@example.com"


@pytest.fixture(autouse=True)
def _tools_registered():
    register_builtin_tools()


@pytest_asyncio.fixture(autouse=True)
async def _use_app_factory(app_session_factory):
    """Completed finalize runs on a FRESH get_session_factory() session (tr-sgz),
    so the global app factory must be initialized for these direct-call tests."""
    yield


class _RoundProxy:
    def __init__(self, rounds):
        self.rounds = rounds
        self.calls = 0
        # Records the `plugins` kwarg seen on each proxy call (tr-uom): the
        # agentic loop must forward the web plugin just like the single-shot path.
        self.plugins_seen: list = []

    def proxy_to_openrouter_with_retry(
        self, messages, model_id, max_retries=3, plugins=None, reasoning_budget=None, tools=None
    ):
        self.plugins_seen.append(plugins)
        chunks = self.rounds[min(self.calls, len(self.rounds) - 1)]
        self.calls += 1

        async def gen():
            for c in chunks:
                yield c

        return gen()


@pytest_asyncio.fixture
async def user(db_session: AsyncSession) -> User:
    u = (
        await db_session.execute(select(User).where(User.email_lower == TEST_EMAIL))
    ).scalar_one_or_none()
    if u is None:
        u = User(
            email=TEST_EMAIL, display_name="Тул", password_hash=None, role="user",
            status="active", balance_micro_rub=10_000_000_000, reserved_micro_rub=0,
        )
        db_session.add(u)
        await db_session.commit()
        await db_session.refresh(u)
    return u


@pytest_asyncio.fixture
async def conversation(db_session: AsyncSession, user: User) -> Conversation:
    conv = Conversation(user_id=user.id, model_id=MODEL_ID, title="Tools")
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


async def _set_web_search(db: AsyncSession, value: bool) -> None:
    await db.execute(
        update(ModelPricing)
        .where(ModelPricing.model_id == MODEL_ID, ModelPricing.is_current.is_(True))
        .values(supports_web_search=value)
    )
    await db.commit()


async def _collect(gen) -> list[SSEEvent]:
    return [e async for e in gen]


def _tc(index, name=None, args=None, id=None):
    return StreamChunk(
        type="tool_call", tool_call_index=index, tool_call_id=id, name=name, arguments_delta=args
    )


CREATE_ROUND = [
    _tc(0, name="create_file", id="c1", args='{"path":"a.md",'),
    _tc(0, args='"file_text":"l1\\nl2"}'),
    StreamChunk(type="usage", input_tokens=20, output_tokens=10),
    StreamChunk(type="done"),
]
ANSWER_ROUND = [
    StreamChunk(type="content", delta="Готово!"),
    StreamChunk(type="usage", input_tokens=30, output_tokens=8),
    StreamChunk(type="done"),
]


# tr-7br: round 0 streams a preamble («…в файл:») AND fires a create_file call;
# round 1 streams the answer («На скриншоте…»). Without a round-boundary separator
# the two blobs concatenate into «…в файл:На скриншоте…» (the reported bug).
PREAMBLE_CREATE_ROUND = [
    StreamChunk(type="content", delta="Сейчас сохраню описание в файл:"),
    _tc(0, name="create_file", id="c1", args='{"path":"a.md",'),
    _tc(0, args='"file_text":"l1\\nl2"}'),
    StreamChunk(type="usage", input_tokens=20, output_tokens=10),
    StreamChunk(type="done"),
]
VISION_ANSWER_ROUND = [
    StreamChunk(type="content", delta="На скриншоте показана таблица."),
    StreamChunk(type="usage", input_tokens=30, output_tokens=8),
    StreamChunk(type="done"),
]


async def test_content_across_rounds_gets_paragraph_separator(
    db_session: AsyncSession, redis_client: Redis, user: User, conversation: Conversation
):
    """tr-7br: when the model emits content in round 0 (a preamble), calls a tool,
    then emits content in round 1 (the answer), the two blobs must NOT collide.
    A paragraph separator («\\n\\n») is inserted into BOTH the live SSE stream and
    the persisted assistant message so live render and reload agree."""
    await _set_tools(db_session, True)
    proxy = _RoundProxy([PREAMBLE_CREATE_ROUND, VISION_ANSWER_ROUND])
    events = await _collect(process_chat_request(
        user_id=user.id, conversation_id=conversation.id,
        message_content="опиши скриншот и сохрани в a.md", model_id=MODEL_ID,
        db=db_session, redis=redis_client, proxy=proxy, tools_enabled=True,
    ))

    # The live stream carries the separator as its own content_delta.
    content_deltas = [e.data["delta"] for e in events if e.event == "content_delta"]
    assert "\n\n" in content_deltas, "no separator content_delta was streamed"
    live = "".join(content_deltas)
    assert "в файл:\n\nНа скриншоте" in live
    assert "файл:На" not in live

    # The PERSISTED assistant content has the same separator (reload parity).
    db_session.expunge_all()
    msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conversation.id, Message.role == "assistant"
            )
        )
    ).scalar_one()
    assert "в файл:\n\nНа скриншоте" in msg.content
    assert "файл:На" not in msg.content


async def test_single_round_content_has_no_spurious_separator(
    db_session: AsyncSession, redis_client: Redis, user: User, conversation: Conversation
):
    """tr-7br guard: a single content reply after one tool round (no second content
    block) must be byte-identical — the round-boundary separator only fires when
    content actually RESUMES, never on the first/only content block."""
    await _set_tools(db_session, True)
    proxy = _RoundProxy([CREATE_ROUND, ANSWER_ROUND])
    events = await _collect(process_chat_request(
        user_id=user.id, conversation_id=conversation.id,
        message_content="сделай файл a.md", model_id=MODEL_ID,
        db=db_session, redis=redis_client, proxy=proxy, tools_enabled=True,
    ))
    content = "".join(e.data["delta"] for e in events if e.event == "content_delta")
    assert content == "Готово!"  # no leading/embedded separator

    db_session.expunge_all()
    msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conversation.id, Message.role == "assistant"
            )
        )
    ).scalar_one()
    assert msg.content == "Готово!"


async def test_tool_loop_halts_on_budget_overrun(
    db_session: AsyncSession, redis_client: Redis, user: User, conversation: Conversation
):
    """tr-rkl / audit M1: once the accumulated cost would exceed the reservation
    (+7% grace), the agentic loop HALTS instead of burning more rounds (e.g. a
    stuck cross-turn edit — tr-7ml). Without the wired budget_guard the loop runs
    to max_rounds and the overdraft is silently absorbed at finalize.

    Round 0 emits a tool call (so the loop WOULD continue) plus a usage chunk far
    larger than the small reservation; the guard checked before round 1 must trip."""
    await _set_tools(db_session, True)
    buster_round = [
        _tc(0, name="create_file", id="c1", args='{"path":"b.md","file_text":"x"}'),
        StreamChunk(type="usage", input_tokens=5_000_000, output_tokens=5_000_000),
        StreamChunk(type="done"),
    ]
    # A second round is available; the guard must stop us reaching it.
    proxy = _RoundProxy([buster_round, ANSWER_ROUND])
    events = await _collect(process_chat_request(
        user_id=user.id, conversation_id=conversation.id,
        message_content="правь файл b.md", model_id=MODEL_ID,
        db=db_session, redis=redis_client, proxy=proxy, tools_enabled=True,
    ))
    names = [e.event for e in events]
    assert "tool_loop_halted" in names
    halted = next(e for e in events if e.event == "tool_loop_halted")
    assert halted.data.get("reason") == "budget"
    # The proxy ran exactly ONE round — the guard stopped round 2+.
    assert proxy.calls == 1


async def test_tool_loop_streams_persists_and_sums_billing(
    db_session: AsyncSession, redis_client: Redis, user: User, conversation: Conversation
):
    await _set_tools(db_session, True)
    proxy = _RoundProxy([CREATE_ROUND, ANSWER_ROUND])
    events = await _collect(process_chat_request(
        user_id=user.id, conversation_id=conversation.id,
        message_content="сделай файл a.md", model_id=MODEL_ID,
        db=db_session, redis=redis_client, proxy=proxy, tools_enabled=True,
    ))
    names = [e.event for e in events]
    assert "tool_call_start" in names
    assert "content_delta" in names
    assert names[-1] == "message_done"

    tr = next(e for e in events if e.event == "tool_result")
    assert tr.data["status"] == "done" and tr.data["filename"] == "a.md"
    assert tr.data["diff"] == {"added": 2, "removed": 0}
    content = "".join(e.data["delta"] for e in events if e.event == "content_delta")
    assert content == "Готово!"

    # Billing summed across rounds: input 20+30=50, output 10+8=18.
    done = events[-1]
    assert done.data["tokens_used"]["input"] == 50
    assert done.data["tokens_used"]["output"] == 18

    db_session.expunge_all()
    msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conversation.id, Message.role == "assistant"
            )
        )
    ).scalar_one()
    assert msg.tool_steps and msg.tool_steps[0]["tool"] == "create_file"
    assert msg.tool_steps[0]["status"] == "done"
    art = (
        await db_session.execute(
            select(Artifact).where(Artifact.conversation_id == conversation.id)
        )
    ).scalar_one()
    assert art.filename == "a.md" and art.message_id == msg.id


async def test_tool_mode_persists_openrouter_generation_id(
    db_session: AsyncSession, redis_client: Redis, user: User, conversation: Conversation
):
    """tr-xzy: tool-mode usage events must thread OpenRouter's generation id to the
    usage_log so its cost can be reconciled against OpenRouter — like the single-shot
    path (last-wins). Charging is unchanged; only the id is persisted."""
    user_id = user.id
    conv_id = conversation.id
    await _set_tools(db_session, True)
    create_round = [
        _tc(0, name="create_file", id="c1", args='{"path":"g.md",'),
        _tc(0, args='"file_text":"x"}'),
        StreamChunk(type="usage", input_tokens=20, output_tokens=10, openrouter_request_id="gen-round-0"),
        StreamChunk(type="done"),
    ]
    answer_round = [
        StreamChunk(type="content", delta="Готово!"),
        StreamChunk(type="usage", input_tokens=30, output_tokens=8, openrouter_request_id="gen-round-1"),
        StreamChunk(type="done"),
    ]
    proxy = _RoundProxy([create_round, answer_round])
    await _collect(process_chat_request(
        user_id=user_id, conversation_id=conv_id,
        message_content="сделай файл", model_id=MODEL_ID,
        db=db_session, redis=redis_client, proxy=proxy, tools_enabled=True,
    ))

    db_session.expunge_all()
    from app.models.usage_log import UsageLog

    usage = (
        await db_session.execute(select(UsageLog).where(UsageLog.user_id == user_id))
    ).scalar_one()
    # last-wins parity with the single-shot path: the final answer round's id.
    assert usage.openrouter_request_id == "gen-round-1"
    msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conv_id, Message.role == "assistant"
            )
        )
    ).scalar_one()
    assert msg is not None  # answer persisted, charge path intact


async def test_tools_enabled_but_unsupported_model_is_single_shot(
    db_session: AsyncSession, redis_client: Redis, user: User, conversation: Conversation
):
    # supports_tools=False → gate keeps the single-shot path; tool events never appear.
    await _set_tools(db_session, False)
    proxy = _RoundProxy([ANSWER_ROUND])
    events = await _collect(process_chat_request(
        user_id=user.id, conversation_id=conversation.id,
        message_content="привет", model_id=MODEL_ID,
        db=db_session, redis=redis_client, proxy=proxy, tools_enabled=True,
    ))
    names = [e.event for e in events]
    assert "tool_call_start" not in names
    assert "tool_result" not in names
    assert names[-1] == "message_done"
    assert proxy.calls == 1  # single round


# One create_file round per index — distinct paths so each round's signature
# differs (dodging the degenerate guard) and the loop runs until max_rounds.
def _create_round(i):
    return [
        _tc(0, name="create_file", id=f"c{i}", args=f'{{"path":"f{i}.md","file_text":"x"}}'),
        StreamChunk(type="usage", input_tokens=1, output_tokens=1),
        StreamChunk(type="done"),
    ]


async def test_max_rounds_emits_halted_sse_and_finalizes(
    db_session: AsyncSession, redis_client: Redis, user: User, conversation: Conversation
):
    """The model that never stops calling tools (8 distinct rounds) trips the
    max_rounds bound: a `tool_loop_halted` SSE is emitted AND billing still
    finalizes with a terminal `message_done` (integration of §6/§7)."""
    await _set_tools(db_session, True)
    # engine.MAX_ROUNDS_DEFAULT = 8 → 8 distinct rounds exhaust the bound.
    proxy = _RoundProxy([_create_round(i) for i in range(8)])
    events = await _collect(process_chat_request(
        user_id=user.id, conversation_id=conversation.id,
        message_content="делай файлы", model_id=MODEL_ID,
        db=db_session, redis=redis_client, proxy=proxy, tools_enabled=True,
    ))
    names = [e.event for e in events]
    assert "tool_loop_halted" in names
    halted = next(e for e in events if e.event == "tool_loop_halted")
    assert halted.data["reason"] == "max_rounds"
    # Still finalized despite the halt.
    assert names[-1] == "message_done"


async def test_degenerate_loop_emits_halted_sse(
    db_session: AsyncSession, redis_client: Redis, user: User, conversation: Conversation
):
    """A model that repeats the SAME tool call trips the degenerate-loop guard
    (identical canonical-JSON K=3): a `tool_loop_halted` SSE with reason
    `degenerate_loop` is emitted and billing still finalizes."""
    await _set_tools(db_session, True)
    # A single repeating round (the proxy replays its last round) → same signature
    # each round → degenerate guard halts at K=3.
    same_round = [
        _tc(0, name="create_file", id="c", args='{"path":"dup.md","file_text":"same"}'),
        StreamChunk(type="usage", input_tokens=1, output_tokens=1),
        StreamChunk(type="done"),
    ]
    proxy = _RoundProxy([same_round])
    events = await _collect(process_chat_request(
        user_id=user.id, conversation_id=conversation.id,
        message_content="зациклись", model_id=MODEL_ID,
        db=db_session, redis=redis_client, proxy=proxy, tools_enabled=True,
    ))
    names = [e.event for e in events]
    assert "tool_loop_halted" in names
    halted = next(e for e in events if e.event == "tool_loop_halted")
    assert halted.data["reason"] == "degenerate_loop"
    assert names[-1] == "message_done"
    assert proxy.calls == 3  # halted at K=3, not run forever


async def test_web_plugin_forwarded_in_tool_mode(
    db_session: AsyncSession, redis_client: Redis, user: User, conversation: Conversation
):
    """tr-uom (F-02): with BOTH tools AND web-search enabled on a web-capable
    model, the agentic loop must forward the web plugin to the proxy on EVERY
    round — otherwise the model silently loses web access in tool mode while the
    single-shot path keeps it. Asserts the recorded `plugins` kwarg carries
    `{"id": "web"}` on each call."""
    await _set_tools(db_session, True)
    await _set_web_search(db_session, True)
    proxy = _RoundProxy([CREATE_ROUND, ANSWER_ROUND])
    await _collect(process_chat_request(
        user_id=user.id, conversation_id=conversation.id,
        message_content="найди свежие данные и сделай файл a.md", model_id=MODEL_ID,
        db=db_session, redis=redis_client, proxy=proxy,
        tools_enabled=True, web_search_enabled=True,
    ))
    # Both rounds called the proxy, and each saw the web plugin forwarded.
    assert proxy.calls == 2
    assert proxy.plugins_seen, "proxy was never called"
    for seen in proxy.plugins_seen:
        assert seen is not None, "plugins kwarg dropped in tool mode"
        assert any(p.get("id") == "web" for p in seen), (
            f"web plugin not forwarded in tool mode: {seen}"
        )
