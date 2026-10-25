"""OpenRouterProxy tests — the OpenAI SDK is mocked, never hits a real API."""
import pytest
import pytest_asyncio
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

import openai

import app.services.openrouter_proxy as proxy_module
from app.core.exceptions import (
    OpenRouterClientError,
    OpenRouterRateLimitError,
    OpenRouterServerError,
    PaidModelsUnavailableError,
)
from app.core.security import encrypt_key
from app.models.api_key import ApiKey
from app.models.model_pricing import ModelPricing
from app.services.openrouter_proxy import (
    OpenRouterProxy,
    ReasoningPiece,
    _extract_reasoning_segments,
    _split_flat_reasoning,
    _strip_control_tokens,
)
from sqlalchemy import select

MODEL_ID = "openai/gpt-4.1-mini"


# ── Fake OpenAI SDK ──────────────────────────────────────────────────────────


class _FakeFunction:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, index=0, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = _FakeFunction(name, arguments)


class _FakeDelta:
    def __init__(
        self, content=None, reasoning=None, reasoning_details=None, tool_calls=None
    ):
        self.content = content
        # OpenAI-style models surface thinking on the flat delta.reasoning string;
        # Anthropic streams it as structured delta.reasoning_details entries.
        self.reasoning = reasoning
        self.reasoning_details = reasoning_details
        # Streamed tool calls (delta.tool_calls[]) with incremental arguments.
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, content=None, reasoning=None, reasoning_details=None, tool_calls=None):
        self.delta = _FakeDelta(content, reasoning, reasoning_details, tool_calls)


class _FakeCompletionDetails:
    def __init__(self, reasoning_tokens):
        self.reasoning_tokens = reasoning_tokens


class _FakeUsage:
    def __init__(self, prompt, completion, cost=None, reasoning_tokens=None):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        # OpenRouter always includes `cost` (USD) in the final usage chunk.
        self.cost = cost
        # Reasoning portion of completion_tokens (None when not applicable).
        self.completion_tokens_details = (
            _FakeCompletionDetails(reasoning_tokens)
            if reasoning_tokens is not None
            else None
        )


class _FakeChunk:
    def __init__(self, content=None, usage=None, reasoning=None, reasoning_details=None, tool_calls=None, id="gen-test123"):
        self.id = id
        self.choices = (
            [_FakeChoice(content, reasoning, reasoning_details, tool_calls)]
            if (
                content is not None
                or reasoning is not None
                or reasoning_details is not None
                or tool_calls is not None
            )
            else []
        )
        self.usage = usage


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        async def _gen():
            for c in self._chunks:
                yield c

        return _gen()


class _FakeAPIStatusError(openai.APIStatusError):
    """openai.APIStatusError that skips the heavy parent __init__.

    isinstance(e, openai.APIStatusError) still holds via the MRO.
    """

    def __init__(self, status_code: int, retry_after: int | None = None):
        Exception.__init__(self, f"mock {status_code}")
        self.status_code = status_code

        class _Resp:
            def __init__(self):
                self.status_code = status_code
                self.headers = (
                    {"Retry-After": str(retry_after)} if retry_after is not None else {}
                )

        self.response = _Resp()
        self.body = None


def _install_openai_mock(monkeypatch, side_effects: list) -> dict:
    """Each item in side_effects is consumed per create() call:
       - list of fake chunks  → returned as a streaming response, or
       - BaseException        → raised from create()
    Returns a dict with `calls: list[dict]` capturing each create() kwargs.
    """
    state: dict = {"calls": []}

    class _Completions:
        async def create(self, **kw):
            state["calls"].append(kw)
            idx = len(state["calls"]) - 1
            if idx >= len(side_effects):
                raise RuntimeError("Test: side_effects exhausted")
            item = side_effects[idx]
            if isinstance(item, BaseException):
                raise item
            return _FakeStream(item)

    class _Chat:
        def __init__(self):
            self.completions = _Completions()

    class _Client:
        def __init__(self, **_kw):
            self.chat = _Chat()

    monkeypatch.setattr(proxy_module.openai, "AsyncOpenAI", _Client)
    return state


async def _no_sleep(_seconds):
    return None


@pytest_asyncio.fixture
async def reset_proxy_state(db_session: AsyncSession):
    await db_session.execute(sa_delete(ApiKey))
    await db_session.commit()
    yield
    await db_session.execute(sa_delete(ApiKey))
    await db_session.commit()


async def _add_active_key(db_session: AsyncSession) -> None:
    db_session.add(ApiKey(
        label="mock",
        encrypted_key=encrypt_key("sk-or-mock-test12345"),
        key_suffix="test1234",
        status="active",
        is_primary=True,
    ))
    await db_session.commit()


# ── Control-token stripping (tr-d17) ─────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Hello.<|eom|>", "Hello."),
        ("<|im_start|>assistant<|im_end|>", "assistant"),
        ("a<|eot_id|>b", "ab"),
        ("<|endoftext|>", ""),
        # Natural prose must be left untouched — no false positives.
        ("normal text, no tokens", "normal text, no tokens"),
        ("a < b and c | d > e", "a < b and c | d > e"),
        ("pipe: |, angle: <, both: <|", "pipe: |, angle: <, both: <|"),
        ("стоимость <100| руб", "стоимость <100| руб"),
        ("", ""),
    ],
)
def test_strip_control_tokens(raw: str, expected: str) -> None:
    assert _strip_control_tokens(raw) == expected


# ── Streaming tests ──────────────────────────────────────────────────────────


async def test_proxy_yields_content_chunks(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    await _add_active_key(db_session)
    chunks = [
        _FakeChunk(content="Hello "),
        _FakeChunk(content="world"),
        _FakeChunk(content="!"),
    ]
    _install_openai_mock(monkeypatch, [chunks])

    proxy = OpenRouterProxy(db_session)
    out = []
    async for c in proxy.proxy_to_openrouter(
        messages=[{"role": "user", "content": "Hi"}],
        model_id=MODEL_ID,
    ):
        out.append(c)

    content = [c for c in out if c.type == "content"]
    assert len(content) == 3
    assert [c.delta for c in content] == ["Hello ", "world", "!"]


async def test_proxy_strips_provider_control_tokens(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """tr-d17: provider special/control tokens (AI21 ``<|eom|>``, Llama
    ``<|eot|>``, ChatML ``<|im_end|>``) must never leak into the visible content
    stream. Live repro: AI21 Jamba ended a reply with ``...content.<|eom|>``."""
    await _add_active_key(db_session)
    chunks = [
        _FakeChunk(content="Файл готов к скачиванию."),
        _FakeChunk(content="<|eom|>"),
        _FakeChunk(content="<|im_end|>tail<|eot|>"),
    ]
    _install_openai_mock(monkeypatch, [chunks])

    proxy = OpenRouterProxy(db_session)
    out = []
    async for c in proxy.proxy_to_openrouter(
        messages=[{"role": "user", "content": "Hi"}],
        model_id=MODEL_ID,
    ):
        out.append(c)

    content = [c for c in out if c.type == "content"]
    joined = "".join(c.delta for c in content)
    assert "<|eom|>" not in joined
    assert "<|im_end|>" not in joined
    assert "<|eot|>" not in joined
    assert joined == "Файл готов к скачиванию.tail"
    # A delta that is ONLY a control token must not surface as an empty chunk.
    assert all(c.delta for c in content)


async def test_proxy_final_chunk_has_usage(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    await _add_active_key(db_session)
    chunks = [
        _FakeChunk(content="hi"),
        _FakeChunk(usage=_FakeUsage(prompt=42, completion=7)),
    ]
    _install_openai_mock(monkeypatch, [chunks])

    proxy = OpenRouterProxy(db_session)
    out = []
    async for c in proxy.proxy_to_openrouter(
        messages=[{"role": "user", "content": "Hi"}],
        model_id=MODEL_ID,
    ):
        out.append(c)

    # Order: content..., usage, done
    types = [c.type for c in out]
    assert types == ["content", "usage", "done"]
    usage_chunk = out[1]
    assert usage_chunk.input_tokens == 42
    assert usage_chunk.output_tokens == 7


async def test_proxy_does_not_hold_open_transaction_during_stream(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """Regression (tr-sgz): the proxy must not keep a DB transaction open across
    the OpenRouter stream.

    proxy_to_openrouter runs get_active_key + a ModelPricing SELECT +
    _increment_key_usage on the request-scoped session BEFORE the streaming loop.
    If that work is left uncommitted, the connection sits 'idle in transaction'
    for the entire (multi-minute) stream, and Postgres's
    idle_in_transaction_session_timeout (60s, added in eb95b3c) terminates the
    backend mid-stream. Finalize then runs `SELECT ... reservations FOR UPDATE`
    on a closed connection (InterfaceError), so the fully-generated reply is never
    persisted and the reservation is released instead of debited. Long answers
    (>60s) hit this; short ones don't."""
    await _add_active_key(db_session)
    chunks = [
        _FakeChunk(content="Hello "),
        _FakeChunk(content="world"),
        _FakeChunk(usage=_FakeUsage(prompt=42, completion=7)),
    ]
    _install_openai_mock(monkeypatch, [chunks])

    proxy = OpenRouterProxy(db_session)
    # Capture the transaction state at each chunk — these are the moments the
    # generator yields control and (in production) would idle for seconds-to-minutes
    # waiting on the next SSE chunk from OpenRouter.
    txn_open_while_streaming: list[bool] = []
    async for c in proxy.proxy_to_openrouter(
        messages=[{"role": "user", "content": "Hi"}],
        model_id=MODEL_ID,
    ):
        txn_open_while_streaming.append(db_session.in_transaction())

    assert txn_open_while_streaming, "proxy yielded no chunks"
    assert not any(txn_open_while_streaming), (
        "proxy held an open DB transaction while streaming; on a long stream "
        "idle_in_transaction_session_timeout will terminate the connection before "
        "finalize runs"
    )


async def test_proxy_requests_usage_accounting(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """Every forwarded request must ask OpenRouter for usage accounting.

    Without `usage: {include: true}` the streamed response omits the usage chunk,
    and billing falls back to estimating OUR context — which misses web-search's
    injected page tokens and the real USD cost. Regression guard for that bug.
    """
    await _add_active_key(db_session)
    state = _install_openai_mock(monkeypatch, [[_FakeChunk(content="x")]])

    proxy = OpenRouterProxy(db_session)
    async for _ in proxy.proxy_to_openrouter(
        messages=[{"role": "user", "content": "Hi"}],
        model_id=MODEL_ID,
    ):
        pass

    extra_body = state["calls"][0].get("extra_body", {})
    assert extra_body.get("usage") == {"include": True}


async def test_proxy_usage_accounting_coexists_with_plugins(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """Web-search plugin and usage accounting both ride in extra_body."""
    await _add_active_key(db_session)
    state = _install_openai_mock(monkeypatch, [[_FakeChunk(content="x")]])

    proxy = OpenRouterProxy(db_session)
    async for _ in proxy.proxy_to_openrouter(
        messages=[{"role": "user", "content": "Hi"}],
        model_id=MODEL_ID,
        plugins=[{"id": "web"}],
    ):
        pass

    extra_body = state["calls"][0].get("extra_body", {})
    assert extra_body.get("usage") == {"include": True}
    assert extra_body.get("plugins") == [{"id": "web"}]


async def test_proxy_done_chunk_last(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    await _add_active_key(db_session)
    chunks = [_FakeChunk(content="x")]
    _install_openai_mock(monkeypatch, [chunks])

    proxy = OpenRouterProxy(db_session)
    out = []
    async for c in proxy.proxy_to_openrouter(
        messages=[{"role": "user", "content": "Hi"}],
        model_id=MODEL_ID,
    ):
        out.append(c)

    assert out[-1].type == "done"


# ── Retry tests ──────────────────────────────────────────────────────────────


async def test_retry_on_5xx(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    await _add_active_key(db_session)
    state = _install_openai_mock(monkeypatch, [
        _FakeAPIStatusError(500),
        [_FakeChunk(content="ok")],
    ])
    monkeypatch.setattr(proxy_module.asyncio, "sleep", _no_sleep)

    proxy = OpenRouterProxy(db_session)
    out = []
    async for c in proxy.proxy_to_openrouter_with_retry(
        messages=[{"role": "user", "content": "Hi"}],
        model_id=MODEL_ID,
        max_retries=3,
    ):
        out.append(c)

    assert len(state["calls"]) == 2
    assert any(c.type == "content" and c.delta == "ok" for c in out)


async def test_no_retry_on_4xx(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    await _add_active_key(db_session)
    state = _install_openai_mock(monkeypatch, [
        _FakeAPIStatusError(400),
        [_FakeChunk(content="should not reach")],
    ])
    monkeypatch.setattr(proxy_module.asyncio, "sleep", _no_sleep)

    proxy = OpenRouterProxy(db_session)
    with pytest.raises(OpenRouterClientError):
        async for _ in proxy.proxy_to_openrouter_with_retry(
            messages=[{"role": "user", "content": "Hi"}],
            model_id=MODEL_ID,
            max_retries=3,
        ):
            pass

    assert len(state["calls"]) == 1


async def test_all_retries_exhausted(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    await _add_active_key(db_session)
    state = _install_openai_mock(monkeypatch, [
        _FakeAPIStatusError(500),
        _FakeAPIStatusError(502),
        _FakeAPIStatusError(503),
        _FakeAPIStatusError(500),
    ])
    monkeypatch.setattr(proxy_module.asyncio, "sleep", _no_sleep)

    proxy = OpenRouterProxy(db_session)
    with pytest.raises(OpenRouterServerError):
        async for _ in proxy.proxy_to_openrouter_with_retry(
            messages=[{"role": "user", "content": "Hi"}],
            model_id=MODEL_ID,
            max_retries=3,
        ):
            pass

    # 1 initial + 3 retries = 4 calls
    assert len(state["calls"]) == 4


async def test_no_active_key_raises_503(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    # No ApiKey row in DB (reset_proxy_state cleared it).
    state = _install_openai_mock(monkeypatch, [
        [_FakeChunk(content="should not reach")],
    ])

    proxy = OpenRouterProxy(db_session)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        async for _ in proxy.proxy_to_openrouter(
            messages=[{"role": "user", "content": "Hi"}],
            model_id=MODEL_ID,
        ):
            pass

    assert exc_info.value.status_code == 503
    # OpenAI mock was never called
    assert state["calls"] == []


# ── Paid-model production-readiness (FEATURE 7) ──────────────────────────────
# These prove the paid path end-to-end with the OpenAI SDK fully mocked — no
# real OpenRouter call, no credit spend.


async def _add_model(
    db: AsyncSession,
    model_id: str,
    *,
    input_price: int,
    output_price: int,
    max_output_tokens: int | None,
) -> None:
    db.add(ModelPricing(
        model_id=model_id,
        model_name=model_id,
        provider="test",
        openrouter_input_price_micro_usd=0,
        openrouter_output_price_micro_usd=0,
        input_price_micro_rub=input_price,
        output_price_micro_rub=output_price,
        max_context_tokens=8192,
        max_output_tokens=max_output_tokens,
        status="available",
        is_current=True,
    ))
    await db.commit()


async def test_usage_cost_parsed_into_chunk(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """OpenRouter's real USD cost (usage.cost) flows into StreamChunk.cost_usd."""
    await _add_active_key(db_session)
    chunks = [
        _FakeChunk(content="hi"),
        _FakeChunk(usage=_FakeUsage(prompt=99, completion=30, cost=0.000042)),
    ]
    _install_openai_mock(monkeypatch, [chunks])

    proxy = OpenRouterProxy(db_session)
    out = [
        c
        async for c in proxy.proxy_to_openrouter(
            messages=[{"role": "user", "content": "Hi"}], model_id=MODEL_ID
        )
    ]
    usage_chunk = next(c for c in out if c.type == "usage")
    assert usage_chunk.cost_usd == 0.000042


async def test_max_tokens_forwarded_from_pricing(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """max_output_tokens from pricing is sent as max_tokens (BUG 7 fix)."""
    await _add_active_key(db_session)
    model_id = "test/paid-maxtok"
    await _add_model(
        db_session, model_id, input_price=1000, output_price=1000, max_output_tokens=1234
    )
    state = _install_openai_mock(monkeypatch, [[_FakeChunk(content="x")]])

    try:
        proxy = OpenRouterProxy(db_session)
        async for _ in proxy.proxy_to_openrouter(
            messages=[{"role": "user", "content": "Hi"}], model_id=model_id
        ):
            pass
        assert state["calls"][0]["max_tokens"] == 1234
    finally:
        await db_session.execute(
            sa_delete(ModelPricing).where(ModelPricing.model_id == model_id)
        )
        await db_session.commit()


async def test_402_paid_model_raises_paid_unavailable(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """402 on a paid model surfaces the friendly 'paid models unavailable' error."""
    await _add_active_key(db_session)
    _install_openai_mock(monkeypatch, [_FakeAPIStatusError(402)])

    proxy = OpenRouterProxy(db_session)
    with pytest.raises(PaidModelsUnavailableError):
        async for _ in proxy.proxy_to_openrouter(
            messages=[{"role": "user", "content": "Hi"}], model_id=MODEL_ID
        ):
            pass


async def test_402_free_model_not_paid_unavailable(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """402 on a :free model is a generic client error, NOT the paid-models message."""
    await _add_active_key(db_session)
    model_id = "test/free-402:free"
    await _add_model(
        db_session, model_id, input_price=0, output_price=0, max_output_tokens=None
    )
    _install_openai_mock(monkeypatch, [_FakeAPIStatusError(402)])

    try:
        proxy = OpenRouterProxy(db_session)
        with pytest.raises(OpenRouterClientError) as exc_info:
            async for _ in proxy.proxy_to_openrouter(
                messages=[{"role": "user", "content": "Hi"}], model_id=model_id
            ):
                pass
        assert not isinstance(exc_info.value, PaidModelsUnavailableError)
    finally:
        await db_session.execute(
            sa_delete(ModelPricing).where(ModelPricing.model_id == model_id)
        )
        await db_session.commit()


async def test_successful_request_increments_key_usage(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """Each forwarded request bumps the primary key's total_requests (BUG 12)."""
    await _add_active_key(db_session)
    _install_openai_mock(monkeypatch, [[_FakeChunk(content="ok")]])

    proxy = OpenRouterProxy(db_session)
    async for _ in proxy.proxy_to_openrouter(
        messages=[{"role": "user", "content": "Hi"}], model_id=MODEL_ID
    ):
        pass

    db_session.expire_all()
    total = (
        await db_session.execute(
            select(ApiKey.total_requests).where(ApiKey.is_primary.is_(True))
        )
    ).scalar_one()
    assert total == 1


# ── Extended thinking / reasoning ────────────────────────────────────────────


async def test_reasoning_budget_sets_extra_body(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """reasoning_budget is forwarded as extra_body.reasoning.max_tokens."""
    await _add_active_key(db_session)
    state = _install_openai_mock(monkeypatch, [[_FakeChunk(content="x")]])

    proxy = OpenRouterProxy(db_session)
    async for _ in proxy.proxy_to_openrouter(
        messages=[{"role": "user", "content": "Hi"}],
        model_id=MODEL_ID,
        reasoning_budget=8192,
    ):
        pass

    extra_body = state["calls"][0].get("extra_body", {})
    assert extra_body.get("reasoning") == {"max_tokens": 8192}
    # usage accounting still rides alongside.
    assert extra_body.get("usage") == {"include": True}


async def test_no_reasoning_budget_excludes_reasoning(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """Without a budget we ask OpenRouter to EXCLUDE reasoning, so always-on
    reasoning models don't return a thinking trace the user didn't request."""
    await _add_active_key(db_session)
    state = _install_openai_mock(monkeypatch, [[_FakeChunk(content="x")]])

    proxy = OpenRouterProxy(db_session)
    async for _ in proxy.proxy_to_openrouter(
        messages=[{"role": "user", "content": "Hi"}],
        model_id=MODEL_ID,
    ):
        pass

    extra_body = state["calls"][0].get("extra_body", {})
    assert extra_body.get("reasoning") == {"exclude": True}


async def test_reasoning_not_requested_suppresses_stream_and_tokens(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """A reasoning-native model that emits reasoning by default must NOT surface
    a thinking trace (chunks) or reasoning tokens when thinking was not requested
    (symptom 2: gpt-oss showed a thinking block with the toggle OFF)."""
    await _add_active_key(db_session)
    chunks = [
        _FakeChunk(reasoning="internal chain-of-thought "),  # emitted unprompted
        _FakeChunk(content="42"),
        _FakeChunk(usage=_FakeUsage(prompt=5, completion=20, reasoning_tokens=15)),
    ]
    _install_openai_mock(monkeypatch, [chunks])

    proxy = OpenRouterProxy(db_session)
    out = [
        c
        async for c in proxy.proxy_to_openrouter(
            messages=[{"role": "user", "content": "Hi"}], model_id=MODEL_ID
        )
    ]
    # No reasoning chunks surface, and the usage chunk reports zero reasoning.
    assert [c for c in out if c.type == "reasoning"] == []
    usage_chunk = next(c for c in out if c.type == "usage")
    assert (usage_chunk.reasoning_tokens or 0) == 0


async def test_anthropic_reasoning_details_yields_reasoning(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """Anthropic streams reasoning via delta.reasoning_details (not the flat
    delta.reasoning). The proxy must extract its text (symptom 1: Claude showed
    no thinking block)."""
    await _add_active_key(db_session)
    chunks = [
        _FakeChunk(reasoning_details=[{"type": "reasoning.text", "text": "Шаг 1. "}]),
        _FakeChunk(reasoning_details=[{"type": "reasoning.text", "text": "Шаг 2."}]),
        _FakeChunk(content="Ответ"),
        _FakeChunk(usage=_FakeUsage(prompt=10, completion=30, reasoning_tokens=12)),
    ]
    _install_openai_mock(monkeypatch, [chunks])

    proxy = OpenRouterProxy(db_session)
    out = [
        c
        async for c in proxy.proxy_to_openrouter(
            messages=[{"role": "user", "content": "Hi"}],
            model_id=MODEL_ID,
            reasoning_budget=2048,
        )
    ]
    reasoning = [c for c in out if c.type == "reasoning"]
    assert [c.delta for c in reasoning] == ["Шаг 1. ", "Шаг 2."]


async def test_reasoning_request_guarantees_max_tokens_above_budget(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """Anthropic requires the overall max_tokens to exceed the thinking budget.
    The synced catalog leaves max_output_tokens NULL, so the proxy must still
    send a max_tokens that is greater than the budget."""
    await _add_active_key(db_session)
    model_id = "test/reasoning-null-maxout"
    await _add_model(
        db_session, model_id, input_price=1000, output_price=1000, max_output_tokens=None
    )
    state = _install_openai_mock(monkeypatch, [[_FakeChunk(content="x")]])

    try:
        proxy = OpenRouterProxy(db_session)
        async for _ in proxy.proxy_to_openrouter(
            messages=[{"role": "user", "content": "Hi"}],
            model_id=model_id,
            reasoning_budget=16384,
        ):
            pass
        call = state["calls"][0]
        assert call["extra_body"]["reasoning"] == {"max_tokens": 16384}
        assert call["max_tokens"] > 16384
    finally:
        await db_session.execute(
            sa_delete(ModelPricing).where(ModelPricing.model_id == model_id)
        )
        await db_session.commit()


async def test_reasoning_budget_clamped_below_max_output(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """Budget is clamped below the model's max_output_tokens (Anthropic requires
    budget_tokens < max_tokens)."""
    await _add_active_key(db_session)
    model_id = "test/reasoning-clamp"
    await _add_model(
        db_session, model_id, input_price=1000, output_price=1000, max_output_tokens=4000
    )
    state = _install_openai_mock(monkeypatch, [[_FakeChunk(content="x")]])

    try:
        proxy = OpenRouterProxy(db_session)
        async for _ in proxy.proxy_to_openrouter(
            messages=[{"role": "user", "content": "Hi"}],
            model_id=model_id,
            reasoning_budget=16384,  # exceeds max_output_tokens=4000
        ):
            pass
        sent = state["calls"][0]["extra_body"]["reasoning"]["max_tokens"]
        assert sent < 4000
    finally:
        await db_session.execute(
            sa_delete(ModelPricing).where(ModelPricing.model_id == model_id)
        )
        await db_session.commit()


async def test_proxy_yields_reasoning_chunks(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """delta.reasoning surfaces as a 'reasoning' StreamChunk, ahead of content
    (when reasoning was requested)."""
    await _add_active_key(db_session)
    chunks = [
        _FakeChunk(reasoning="Let me think… "),
        _FakeChunk(reasoning="9.11 < 9.9."),
        _FakeChunk(content="9.9 is bigger."),
        _FakeChunk(usage=_FakeUsage(prompt=10, completion=30, reasoning_tokens=12)),
    ]
    _install_openai_mock(monkeypatch, [chunks])

    proxy = OpenRouterProxy(db_session)
    out = [
        c
        async for c in proxy.proxy_to_openrouter(
            messages=[{"role": "user", "content": "Hi"}],
            model_id=MODEL_ID,
            reasoning_budget=8192,
        )
    ]

    reasoning = [c for c in out if c.type == "reasoning"]
    assert [c.delta for c in reasoning] == ["Let me think… ", "9.11 < 9.9."]
    content = [c for c in out if c.type == "content"]
    assert [c.delta for c in content] == ["9.9 is bigger."]


async def test_usage_reports_reasoning_tokens(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """usage.completion_tokens_details.reasoning_tokens flows into the usage chunk
    when reasoning was requested."""
    await _add_active_key(db_session)
    chunks = [
        _FakeChunk(content="ok"),
        _FakeChunk(usage=_FakeUsage(prompt=10, completion=30, reasoning_tokens=12)),
    ]
    _install_openai_mock(monkeypatch, [chunks])

    proxy = OpenRouterProxy(db_session)
    out = [
        c
        async for c in proxy.proxy_to_openrouter(
            messages=[{"role": "user", "content": "Hi"}],
            model_id=MODEL_ID,
            reasoning_budget=8192,
        )
    ]
    usage_chunk = next(c for c in out if c.type == "usage")
    assert usage_chunk.output_tokens == 30
    assert usage_chunk.reasoning_tokens == 12


async def test_proxy_emits_openrouter_generation_id(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """The proxy must surface OpenRouter's gen-… id on its chunks so billing can
    persist it (needed for cost reconciliation of aborted requests, tr-sgz)."""
    await _add_active_key(db_session)
    chunks = [
        _FakeChunk(content="hi", id="gen-abc"),
        _FakeChunk(usage=_FakeUsage(prompt=42, completion=7), id="gen-abc"),
    ]
    _install_openai_mock(monkeypatch, [chunks])

    proxy = OpenRouterProxy(db_session)
    out = []
    async for c in proxy.proxy_to_openrouter(
        messages=[{"role": "user", "content": "Hi"}],
        model_id=MODEL_ID,
    ):
        out.append(c)

    content = next(c for c in out if c.type == "content")
    usage = next(c for c in out if c.type == "usage")
    assert content.openrouter_request_id == "gen-abc"
    assert usage.openrouter_request_id == "gen-abc"


# ── Reasoning segmentation (tr-cy0.3.4) ──────────────────────────────────────


def test_extract_segments_structured_multiblock_in_order() -> None:
    """Anthropic reasoning_details carry a real `index` per block — two indices
    yield two ordered pieces (acceptance #1)."""
    delta = _FakeDelta(
        reasoning_details=[
            {"type": "reasoning.text", "text": "Block A", "index": 0},
            {"type": "reasoning.text", "text": "Block B", "index": 1},
        ]
    )
    pieces = _extract_reasoning_segments(delta)
    assert [(p.index, p.text) for p in pieces] == [(0, "Block A"), (1, "Block B")]


def test_extract_segments_structured_single_block() -> None:
    """A single reasoning_details entry (or entries sharing one index) is one
    segment — no false splitting."""
    delta = _FakeDelta(
        reasoning_details=[{"type": "reasoning.text", "text": "Solo", "index": 0}]
    )
    pieces = _extract_reasoning_segments(delta)
    assert len(pieces) == 1
    assert pieces[0].index == 0 and pieces[0].text == "Solo"


def test_extract_segments_carries_provider_summary() -> None:
    """A reasoning.summary entry surfaces its prose as the piece's summary, with
    no text (acceptance #3)."""
    delta = _FakeDelta(
        reasoning_details=[
            {"type": "reasoning.summary", "summary": "Краткое резюме", "index": 0},
        ]
    )
    pieces = _extract_reasoning_segments(delta)
    assert len(pieces) == 1
    assert pieces[0].summary == "Краткое резюме"
    assert pieces[0].text == ""


def test_extract_segments_skips_encrypted() -> None:
    """reasoning.encrypted entries carry no displayable text and are dropped."""
    delta = _FakeDelta(
        reasoning_details=[{"type": "reasoning.encrypted", "data": "xxx", "index": 0}]
    )
    assert _extract_reasoning_segments(delta) == []


def test_extract_segments_flat_is_single_piece() -> None:
    """Flat (OpenAI-style) reasoning has no provider boundaries: one piece with
    index None (the caller assigns the live ordinal)."""
    delta = _FakeDelta(reasoning="thinking out loud")
    pieces = _extract_reasoning_segments(delta)
    assert pieces == [ReasoningPiece(text="thinking out loud", index=None, summary=None)]


def test_extract_segments_none_when_no_reasoning() -> None:
    assert _extract_reasoning_segments(_FakeDelta(content="answer")) == []


def test_split_flat_reasoning_splits_on_heading_and_bold() -> None:
    """Flat reasoning boundaries are set by markdown-heading / bold lead
    (acceptance #2)."""
    text = (
        "Intro line about the plan.\n"
        "## Step one\nDo the first thing.\n"
        "**Step two:** then the second.\n"
    )
    blocks = _split_flat_reasoning(text)
    assert len(blocks) == 3
    assert blocks[0].startswith("Intro")
    assert blocks[1].startswith("## Step one")
    assert blocks[2].startswith("**Step two")


def test_split_flat_reasoning_no_markers_single_block() -> None:
    assert _split_flat_reasoning("just one continuous thought, no markers") == [
        "just one continuous thought, no markers"
    ]


async def test_structured_stream_emits_segment_breaks(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """Two provider indices across streamed chunks ⇒ exactly two segment breaks,
    ordinals 0 then 1, with text attributed to the right segment."""
    await _add_active_key(db_session)
    chunks = [
        _FakeChunk(reasoning_details=[{"type": "reasoning.text", "text": "A1 ", "index": 0}]),
        _FakeChunk(reasoning_details=[{"type": "reasoning.text", "text": "A2", "index": 0}]),
        _FakeChunk(reasoning_details=[{"type": "reasoning.text", "text": "B1", "index": 1}]),
        _FakeChunk(content="answer"),
        _FakeChunk(usage=_FakeUsage(prompt=5, completion=9, reasoning_tokens=4)),
    ]
    _install_openai_mock(monkeypatch, [chunks])

    proxy = OpenRouterProxy(db_session)
    out = [
        c
        async for c in proxy.proxy_to_openrouter(
            messages=[{"role": "user", "content": "Hi"}],
            model_id=MODEL_ID,
            reasoning_budget=2048,
        )
    ]
    reasoning = [c for c in out if c.type == "reasoning"]
    assert [c.segment_break for c in reasoning] == [True, False, True]
    assert [c.segment_index for c in reasoning] == [0, 0, 1]
    assert [c.delta for c in reasoning] == ["A1 ", "A2", "B1"]


async def test_flat_stream_is_single_segment(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    """Flat reasoning streams as one ongoing segment: a break only on the first
    reasoning chunk, ordinal 0 throughout."""
    await _add_active_key(db_session)
    chunks = [
        _FakeChunk(reasoning="th"),
        _FakeChunk(reasoning="inking"),
        _FakeChunk(content="answer"),
        _FakeChunk(usage=_FakeUsage(prompt=5, completion=9, reasoning_tokens=4)),
    ]
    _install_openai_mock(monkeypatch, [chunks])

    proxy = OpenRouterProxy(db_session)
    out = [
        c
        async for c in proxy.proxy_to_openrouter(
            messages=[{"role": "user", "content": "Hi"}],
            model_id=MODEL_ID,
            reasoning_budget=2048,
        )
    ]
    reasoning = [c for c in out if c.type == "reasoning"]
    assert [c.segment_break for c in reasoning] == [True, False]
    assert [c.segment_index for c in reasoning] == [0, 0]


# ── Tool-call passthrough + streaming (tr-cy0.2.4) ───────────────────────────


async def test_tools_passed_to_create(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    await _add_active_key(db_session)
    state = _install_openai_mock(monkeypatch, [[_FakeChunk(content="x")]])
    tools = [{"type": "function", "function": {"name": "create_file"}}]

    proxy = OpenRouterProxy(db_session)
    async for _ in proxy.proxy_to_openrouter(
        messages=[{"role": "user", "content": "Hi"}],
        model_id=MODEL_ID,
        tools=tools,
    ):
        pass
    call = state["calls"][0]
    assert call["tools"] == tools
    assert call["tool_choice"] == "auto"


async def test_no_tools_no_tool_kwargs(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    await _add_active_key(db_session)
    state = _install_openai_mock(monkeypatch, [[_FakeChunk(content="x")]])
    proxy = OpenRouterProxy(db_session)
    async for _ in proxy.proxy_to_openrouter(
        messages=[{"role": "user", "content": "Hi"}], model_id=MODEL_ID
    ):
        pass
    assert "tools" not in state["calls"][0]


async def test_tool_call_deltas_streamed(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_proxy_state,
) -> None:
    await _add_active_key(db_session)
    chunks = [
        _FakeChunk(tool_calls=[_FakeToolCall(index=0, id="call_1", name="create_file", arguments='{"path":')]),
        _FakeChunk(tool_calls=[_FakeToolCall(index=0, arguments='"a.md","file_text":"hi"}')]),
        _FakeChunk(usage=_FakeUsage(prompt=5, completion=9)),
    ]
    _install_openai_mock(monkeypatch, [chunks])

    proxy = OpenRouterProxy(db_session)
    out = [
        c
        async for c in proxy.proxy_to_openrouter(
            messages=[{"role": "user", "content": "Hi"}],
            model_id=MODEL_ID,
            tools=[{"type": "function", "function": {"name": "create_file"}}],
        )
    ]
    tcs = [c for c in out if c.type == "tool_call"]
    assert len(tcs) == 2
    assert tcs[0].tool_call_index == 0
    assert tcs[0].tool_call_id == "call_1"
    assert tcs[0].name == "create_file"
    assert "".join(c.arguments_delta or "" for c in tcs) == '{"path":"a.md","file_text":"hi"}'


# ── Window #1: idle-in-transaction across create() TTFB (tr-sgz) ─────────────


def _install_order_recording_client(monkeypatch, db_session, order, stream_chunks):
    """Patch the OpenAI client so create() records 'create' into `order`, and wrap
    db_session.commit/rollback to record 'commit'/'rollback' — so we can assert the
    transaction boundary relative to the network create() call."""
    real_rollback = db_session.rollback
    real_commit = db_session.commit

    async def _rec_rollback():
        order.append("rollback")
        return await real_rollback()

    async def _rec_commit():
        order.append("commit")
        return await real_commit()

    monkeypatch.setattr(db_session, "rollback", _rec_rollback)
    monkeypatch.setattr(db_session, "commit", _rec_commit)

    class _Completions:
        async def create(self, **kw):
            order.append("create")
            return _FakeStream(stream_chunks)

    class _Chat:
        def __init__(self):
            self.completions = _Completions()

    class _Client:
        def __init__(self, **_kw):
            self.chat = _Chat()

    monkeypatch.setattr(proxy_module.openai, "AsyncOpenAI", _Client)


async def test_single_shot_commits_read_txn_before_create(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, reset_proxy_state
) -> None:
    """Single-shot must END the read-only key/pricing transaction BEFORE the (possibly
    long) create() TTFB, so the connection isn't idle-in-transaction during it
    (idle_in_transaction_session_timeout would otherwise kill a >60s reasoning TTFB).
    It must use commit(), NOT rollback(): rollback would expire the orchestrator's
    shared ORM objects -> MissingGreenlet at finalize (see the expiry test below)."""
    await _add_active_key(db_session)
    order: list[str] = []
    _install_order_recording_client(
        monkeypatch, db_session, order,
        [_FakeChunk(content="hi"), _FakeChunk(usage=_FakeUsage(1, 1))],
    )

    proxy = OpenRouterProxy(db_session)
    async for _ in proxy.proxy_to_openrouter(
        messages=[{"role": "user", "content": "hi"}], model_id=MODEL_ID, tools=None
    ):
        pass

    assert "rollback" not in order, order                          # rollback would expire caller objects
    # Read-txn committed before the create() TTFB, key-usage committed after it.
    assert order == ["commit", "create", "commit"], order


async def test_single_shot_does_not_expire_caller_session_objects(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, reset_proxy_state
) -> None:
    """Regression (tr-sgz): the orchestrator reads `pricing`/`reservation` ORM objects
    off the SHARED request session AFTER the stream (STEP 6). The proxy's window#1
    transaction-close must not EXPIRE them — an expired attribute read fires a sync
    lazy-load -> sqlalchemy MissingGreenlet, crashing finalize and losing the answer.
    rollback() expires; commit() (with expire_on_commit=False) does not."""
    from sqlalchemy import inspect as sa_inspect

    await _add_active_key(db_session)
    pricing = (
        await db_session.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == MODEL_ID, ModelPricing.is_current.is_(True)
            )
        )
    ).scalar_one()
    _ = pricing.input_price_micro_rub  # force the attributes to be loaded
    _install_openai_mock(monkeypatch, [[_FakeChunk(content="hi"), _FakeChunk(usage=_FakeUsage(1, 1))]])

    proxy = OpenRouterProxy(db_session)
    async for _ in proxy.proxy_to_openrouter(
        messages=[{"role": "user", "content": "hi"}], model_id=MODEL_ID, tools=None
    ):
        pass

    assert sa_inspect(pricing).expired is False, "proxy expired the caller's ORM object"
    _ = pricing.input_price_micro_rub  # must not trigger IO / raise MissingGreenlet


async def test_tool_mode_does_not_expire_caller_session_objects(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, reset_proxy_state
) -> None:
    """tr-ans: the new tool-mode pre-create() commit must not EXPIRE the caller's
    shared ORM objects (pricing/reservation) the orchestrator reads at STEP 6 —
    commit() (expire_on_commit=False) doesn't; rollback() would -> MissingGreenlet."""
    from sqlalchemy import inspect as sa_inspect

    await _add_active_key(db_session)
    pricing = (await db_session.execute(
        select(ModelPricing).where(
            ModelPricing.model_id == MODEL_ID, ModelPricing.is_current.is_(True)
        )
    )).scalar_one()
    _ = pricing.input_price_micro_rub
    _install_openai_mock(monkeypatch, [[_FakeChunk(content="hi"), _FakeChunk(usage=_FakeUsage(1, 1))]])

    proxy = OpenRouterProxy(db_session)
    tools = [{"type": "function", "function": {"name": "x", "parameters": {"type": "object"}}}]
    async for _ in proxy.proxy_to_openrouter(
        messages=[{"role": "user", "content": "hi"}], model_id=MODEL_ID, tools=tools
    ):
        pass

    assert sa_inspect(pricing).expired is False, "tool-mode commit expired the caller's ORM object"
    _ = pricing.input_price_micro_rub  # must not raise MissingGreenlet


async def test_tool_mode_commits_read_txn_before_create(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, reset_proxy_state
) -> None:
    """tr-ans: tool mode must ALSO end the read-only key/pricing txn BEFORE the
    create() TTFB (parity with single-shot), so the connection isn't idle-in-
    transaction across a multi-second tool-round TTFB. Safe now because the engine
    commits each tool flush, so no uncommitted artifact lingers for an early commit
    to prematurely persist. Must use commit() (not rollback()) to keep the caller's
    pricing/reservation loaded."""
    await _add_active_key(db_session)
    order: list[str] = []
    _install_order_recording_client(
        monkeypatch, db_session, order,
        [_FakeChunk(content="hi"), _FakeChunk(usage=_FakeUsage(1, 1))],
    )

    proxy = OpenRouterProxy(db_session)
    tools = [{"type": "function", "function": {"name": "x", "parameters": {"type": "object"}}}]
    async for _ in proxy.proxy_to_openrouter(
        messages=[{"role": "user", "content": "hi"}], model_id=MODEL_ID, tools=tools
    ):
        pass

    assert "rollback" not in order, order                  # commit, never rollback (no expiry)
    assert order == ["commit", "create", "commit"], order  # read-txn closed before TTFB; key-usage after


async def test_guard_drop_neutral_to_key_usage_increment(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, reset_proxy_state
) -> None:
    """tr-ans / tr-qyh: dropping the window#1 guard must be NEUTRAL to the
    (pre-existing, out-of-scope) per-create() key-usage increment. One successful
    tool-mode create() increments ApiKey.total_requests exactly once — the change
    does not introduce a NEW double-count."""
    await _add_active_key(db_session)
    _install_openai_mock(monkeypatch, [[_FakeChunk(content="hi"), _FakeChunk(usage=_FakeUsage(1, 1))]])

    proxy = OpenRouterProxy(db_session)
    tools = [{"type": "function", "function": {"name": "x", "parameters": {"type": "object"}}}]
    async for _ in proxy.proxy_to_openrouter(
        messages=[{"role": "user", "content": "hi"}], model_id=MODEL_ID, tools=tools
    ):
        pass

    db_session.expire_all()
    key = (await db_session.execute(select(ApiKey).where(ApiKey.is_primary.is_(True)))).scalar_one()
    assert key.total_requests == 1  # one create() -> one increment (no NEW double-count)


# Suppress an unused-fixture warning by referencing the rate-limit error.
_ = OpenRouterRateLimitError
