"""Agentic loop engine: rounds, tool execution, bounds, error feedback (tr-cy0.2.5)."""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session_factory
from app.models.artifact import Artifact
from app.models.conversation import Conversation
from app.models.user import User
from app.services.openrouter_proxy import StreamChunk
from app.services.tools.engine import run_agentic_loop, _title
from app.services.tools.file_tools import ToolContext, register_builtin_tools

pytestmark = pytest.mark.asyncio


def test_title_view_file_is_russian():
    # tr-7ml: the read tool gets a proper Russian timeline title, like its peers.
    assert _title("view_file", "notes.md") == "Просмотр файла notes.md"
    assert _title("view_file", None) == "Просмотр файла"


def test_title_run_python_is_russian():
    # tr-j20e: code execution gets a static Russian title (no path appended).
    assert _title("run_python", None) == "Выполнение Python"
    assert _title("run_python", "ignored") == "Выполнение Python"


async def test_tool_result_carries_run_python_input_for_reload(db_session, user, conversation):
    """tr-jk4j: persist the submitted code (tool args) for run_python on the
    tool_result event so the run-card can show it after reload. File tools must NOT
    carry input (their args can be large — e.g. create_file's file_text)."""
    from app.schemas.tools import ToolResultPayload
    from app.services.tools import registry
    from app.services.tools.registry import ToolSpec, clear_registry

    async def _fake_run(args, ctx):
        return ToolResultPayload(status="ok", output="ok", duration_ms=5)

    registry.register(
        ToolSpec(
            name="run_python",
            definition={"type": "function", "function": {"name": "run_python"}},
            executor=_fake_run,
            safety_profile="code_execution",
        )
    )
    try:
        proxy = _RoundProxy([
            [_tc(0, name="run_python", id="r1", args='{"code":"print(1)"}'), StreamChunk(type="done")],
            [StreamChunk(type="content", delta="ok"), StreamChunk(type="done")],
        ])
        events = await _collect(proxy, _ctx(db_session, user, conversation))
        tr = next(e for e in events if e.kind == "tool_result")
        assert tr.input == '{"code":"print(1)"}'
    finally:
        clear_registry()
        register_builtin_tools()


async def test_tool_result_omits_input_for_file_tools(db_session, user, conversation):
    """tr-jk4j: create_file (large file_text args) must NOT have its input persisted."""
    proxy = _RoundProxy([
        [
            _tc(0, name="create_file", id="c1", args='{"path":"a.md",'),
            _tc(0, args='"file_text":"l1\\nl2"}'),
            StreamChunk(type="done"),
        ],
        [StreamChunk(type="content", delta="ok"), StreamChunk(type="done")],
    ])
    events = await _collect(proxy, _ctx(db_session, user, conversation))
    tr = next(e for e in events if e.kind == "tool_result")
    assert tr.input is None


async def test_tool_result_carries_execution_metadata(db_session, user, conversation):
    """tr-j20e: the engine surfaces a code-exec tool's duration/stderr/truncated/cost
    on the tool_result EngineEvent so billing (tr-nyil) + the run-card (tr-7so1) can
    use them. Uses a fake executor so the carry-through is tested without the sandbox."""
    from app.schemas.tools import ToolResultPayload
    from app.services.tools import registry
    from app.services.tools.registry import ToolSpec, clear_registry

    async def _fake_exec(args, ctx):
        return ToolResultPayload(
            status="ok",
            output="42",
            stderr="a warning",
            duration_ms=137,
            truncated=True,
            cost_micro_rub=1685,
        )

    registry.register(
        ToolSpec(
            name="run_python",
            definition={"type": "function", "function": {"name": "run_python"}},
            executor=_fake_exec,
            safety_profile="code_execution",
        )
    )
    try:
        proxy = _RoundProxy([
            [_tc(0, name="run_python", id="r1", args="{}"), StreamChunk(type="done")],
            [StreamChunk(type="content", delta="готово"), StreamChunk(type="done")],
        ])
        events = await _collect(proxy, _ctx(db_session, user, conversation))
        tr = next(e for e in events if e.kind == "tool_result")
        assert tr.tool == "run_python"
        assert tr.title == "Выполнение Python"
        assert tr.status == "done"
        assert tr.duration_ms == 137
        assert tr.stderr == "a warning"
        assert tr.truncated is True
        assert tr.cost_micro_rub == 1685
    finally:
        # Restore the registry to the file-tools-only state for later tests.
        clear_registry()
        register_builtin_tools()


async def test_tool_result_carries_rich_results(db_session, user, conversation):
    """tr-5ktl Ф4: the engine surfaces a code-exec tool's rich results[] (matplotlib
    PNG, DataFrame HTML) on the tool_result EngineEvent so the run-card renders them.
    A tool with no rich output leaves results None (file tools unaffected)."""
    from app.schemas.tools import ToolResultPayload
    from app.services.tools import registry
    from app.services.tools.registry import ToolSpec, clear_registry

    rich = [{"type": "image/png", "data": "AAA"}, {"type": "text/html", "data": "<table></table>"}]

    async def _fake_tool(args, ctx):
        return ToolResultPayload(status="ok", output="ok", duration_ms=5, results=rich)

    registry.register(
        ToolSpec(
            name="run_python",
            definition={"type": "function", "function": {"name": "run_python"}},
            executor=_fake_tool,
            safety_profile="code_execution",
        )
    )
    try:
        proxy = _RoundProxy([
            [_tc(0, name="run_python", id="r1", args="{}"), StreamChunk(type="done")],
            [StreamChunk(type="content", delta="готово"), StreamChunk(type="done")],
        ])
        events = await _collect(proxy, _ctx(db_session, user, conversation))
        tr = next(e for e in events if e.kind == "tool_result")
        assert tr.results == rich
    finally:
        clear_registry()
        register_builtin_tools()


async def test_vision_roundtrip_injects_images_for_vision_model(db_session, user, conversation):
    """tr-5ktl Ф4: after a run_python step yields image/png results, a vision-capable
    model gets each image fed back as a user image_url message so it can reason about
    its own chart on the next round."""
    from app.schemas.tools import ToolResultPayload
    from app.services.tools import registry
    from app.services.tools.registry import ToolSpec, clear_registry

    async def _fake_tool(args, ctx):
        return ToolResultPayload(
            status="ok", output="ok", duration_ms=5,
            results=[{"type": "image/png", "data": "PNGDATA"}],
        )

    registry.register(ToolSpec(
        name="run_python",
        definition={"type": "function", "function": {"name": "run_python"}},
        executor=_fake_tool, safety_profile="code_execution",
    ))
    try:
        proxy = _RoundProxy([
            [_tc(0, name="run_python", id="r1", args="{}"), StreamChunk(type="done")],
            [StreamChunk(type="content", delta="вижу график"), StreamChunk(type="done")],
        ])
        await _collect(proxy, _ctx(db_session, user, conversation), vision_capable=True)
        round2 = proxy.captured_messages[1]
        parts = [
            p for m in round2 if isinstance(m.get("content"), list)
            for p in m["content"] if p.get("type") == "image_url"
        ]
        assert any(p["image_url"]["url"] == "data:image/png;base64,PNGDATA" for p in parts)
    finally:
        clear_registry()
        register_builtin_tools()


async def test_vision_roundtrip_skipped_for_nonvision_model(db_session, user, conversation):
    """A non-vision model must NOT get image parts injected (the provider would reject
    them / waste tokens); the run completes with only the text tool result."""
    from app.schemas.tools import ToolResultPayload
    from app.services.tools import registry
    from app.services.tools.registry import ToolSpec, clear_registry

    async def _fake_tool(args, ctx):
        return ToolResultPayload(
            status="ok", output="ok", duration_ms=5,
            results=[{"type": "image/png", "data": "PNGDATA"}],
        )

    registry.register(ToolSpec(
        name="run_python",
        definition={"type": "function", "function": {"name": "run_python"}},
        executor=_fake_tool, safety_profile="code_execution",
    ))
    try:
        proxy = _RoundProxy([
            [_tc(0, name="run_python", id="r1", args="{}"), StreamChunk(type="done")],
            [StreamChunk(type="content", delta="готово"), StreamChunk(type="done")],
        ])
        await _collect(proxy, _ctx(db_session, user, conversation), vision_capable=False)
        round2 = proxy.captured_messages[1]
        assert not any(
            isinstance(m.get("content"), list)
            and any(p.get("type") == "image_url" for p in m["content"])
            for m in round2
        )
    finally:
        clear_registry()
        register_builtin_tools()


@pytest.fixture(autouse=True)
def _ensure_tools_registered():
    # Other tests may clear the global registry; re-register the file tools.
    register_builtin_tools()


class _RoundProxy:
    """proxy_to_openrouter_with_retry yields the next round's chunks per call."""

    def __init__(self, rounds: list[list[StreamChunk]]):
        self.rounds = rounds
        self.calls = 0
        self.captured_tools = None
        self.captured_messages: list[list] = []  # per-round snapshot of the context

    def proxy_to_openrouter_with_retry(
        self, messages, model_id, max_retries=3, plugins=None, reasoning_budget=None, tools=None
    ):
        self.captured_tools = tools
        self.captured_messages.append(list(messages))
        chunks = self.rounds[self.calls]
        self.calls += 1

        async def gen():
            for c in chunks:
                yield c

        return gen()


@pytest_asyncio.fixture
async def user(db_session: AsyncSession) -> User:
    u = User(
        email="engine_test@example.com",
        display_name="Движок",
        password_hash=None,
        role="user",
        status="active",
        balance_micro_rub=1_000_000,
        reserved_micro_rub=0,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


@pytest_asyncio.fixture
async def conversation(db_session: AsyncSession, user: User) -> Conversation:
    conv = Conversation(user_id=user.id, model_id="anthropic/claude-haiku-4-5", title="A")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    return conv


def _ctx(db, user, conv) -> ToolContext:
    return ToolContext(db=db, user_id=user.id, conversation_id=conv.id)


def _tc(index, name=None, args=None, id=None):
    return StreamChunk(
        type="tool_call", tool_call_index=index, tool_call_id=id, name=name, arguments_delta=args
    )


async def _collect(proxy, ctx, **kw):
    return [
        e
        async for e in run_agentic_loop(
            proxy=proxy,
            model_id="m",
            context_messages=[{"role": "user", "content": "hi"}],
            tool_ctx=ctx,
            **kw,
        )
    ]


async def test_one_tool_round_then_answer(db_session, user, conversation):
    proxy = _RoundProxy([
        [
            _tc(0, name="create_file", id="c1", args='{"path":"a.md",'),
            _tc(0, args='"file_text":"l1\\nl2"}'),
            StreamChunk(type="usage", input_tokens=10, output_tokens=5),
            StreamChunk(type="done"),
        ],
        [
            StreamChunk(type="content", delta="Готово."),
            StreamChunk(type="usage", input_tokens=3, output_tokens=4),
            StreamChunk(type="done"),
        ],
    ])
    ctx = _ctx(db_session, user, conversation)
    events = await _collect(proxy, ctx)
    kinds = [e.kind for e in events]

    assert "tool_call_start" in kinds
    tr = next(e for e in events if e.kind == "tool_result")
    assert tr.status == "done" and tr.filename == "a.md"
    assert tr.diff == {"added": 2, "removed": 0}
    assert next(e for e in events if e.kind == "content").delta == "Готово."
    assert kinds[-1] == "done"
    assert len([e for e in events if e.kind == "usage"]) == 2  # one per round
    assert len(ctx.created_artifact_ids) == 1
    # The model was given tool definitions.
    assert proxy.captured_tools and proxy.captured_tools[0]["function"]["name"]


async def test_tool_error_is_fed_back_and_loop_continues(db_session, user, conversation):
    proxy = _RoundProxy([
        [
            _tc(0, name="str_replace", id="c1", args='{"path":"missing.md","old_str":"x","new_str":"y"}'),
            StreamChunk(type="usage", input_tokens=5, output_tokens=5),
            StreamChunk(type="done"),
        ],
        [
            StreamChunk(type="content", delta="Не вышло, но ок."),
            StreamChunk(type="usage", input_tokens=2, output_tokens=2),
            StreamChunk(type="done"),
        ],
    ])
    ctx = _ctx(db_session, user, conversation)
    events = await _collect(proxy, ctx)
    tr = next(e for e in events if e.kind == "tool_result")
    assert tr.status == "error" and "не найден" in tr.output
    # the loop continued to a content round (the error was fed back)
    assert any(e.kind == "content" for e in events)
    assert events[-1].kind == "done"


async def test_max_rounds_halts(db_session, user, conversation):
    rounds = [
        [
            _tc(0, name="create_file", id=f"c{i}", args=f'{{"path":"f{i}.md","file_text":"x"}}'),
            StreamChunk(type="usage", input_tokens=1, output_tokens=1),
            StreamChunk(type="done"),
        ]
        for i in range(2)
    ]
    proxy = _RoundProxy(rounds)
    ctx = _ctx(db_session, user, conversation)
    events = await _collect(proxy, ctx, max_rounds=2)
    assert any(e.kind == "halted" and e.reason == "max_rounds" for e in events)
    assert events[-1].kind == "done"


async def test_degenerate_loop_halts(db_session, user, conversation):
    same = lambda: [
        _tc(0, name="create_file", id="c", args='{"path":"d.md","file_text":"same"}'),
        StreamChunk(type="usage", input_tokens=1, output_tokens=1),
        StreamChunk(type="done"),
    ]
    proxy = _RoundProxy([same(), same(), same()])
    ctx = _ctx(db_session, user, conversation)
    events = await _collect(proxy, ctx, degenerate_k=2)
    assert any(e.kind == "halted" and e.reason == "degenerate_loop" for e in events)
    # halted before exhausting all three rounds
    assert proxy.calls == 2


async def test_engine_commits_artifact_each_round(
    db_session, user, conversation, app_session_factory
):
    """tr-ans window #2: the engine commits each tool's flush immediately, so the
    artifact is visible to a SEPARATE session at the tool_result boundary — proving
    the request connection is released (committed, not just flushed) before the next
    round, and pinning the commit-before-yield ordering prune-parity depends on."""
    proxy = _RoundProxy([
        [
            _tc(0, name="create_file", id="c1", args='{"path":"a.md","file_text":"l1\\nl2"}'),
            StreamChunk(type="usage", input_tokens=10, output_tokens=5),
            StreamChunk(type="done"),
        ],
        [
            StreamChunk(type="content", delta="Готово."),
            StreamChunk(type="usage", input_tokens=3, output_tokens=4),
            StreamChunk(type="done"),
        ],
    ])
    ctx = _ctx(db_session, user, conversation)
    conv_id = conversation.id
    seen_committed = None
    async for e in run_agentic_loop(
        proxy=proxy, model_id="m",
        context_messages=[{"role": "user", "content": "hi"}], tool_ctx=ctx,
    ):
        if e.kind == "tool_result":
            async with get_session_factory()() as other:
                rows = (await other.execute(
                    select(Artifact).where(Artifact.conversation_id == conv_id)
                )).scalars().all()
            seen_committed = len(rows)
            break
    assert seen_committed == 1, (
        "engine must COMMIT the artifact before yielding tool_result "
        f"(separate session saw {seen_committed} rows)"
    )
