import asyncio
import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import httpx
import openai
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import (
    OpenRouterClientError,
    OpenRouterRateLimitError,
    OpenRouterServerError,
    PaidModelsUnavailableError,
)
from app.core.security import decrypt_key
from app.models.api_key import ApiKey
from app.models.model_pricing import ModelPricing


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Anthropic requires a thinking budget of at least 1024 tokens; below this a
# reasoning request is invalid, so we exclude reasoning instead.
_ANTHROPIC_MIN_REASONING_TOKENS = 1024
# Headroom reserved for the visible answer above the thinking budget when the
# model's output window is unknown (max_output_tokens NULL). Anthropic needs the
# overall max_tokens to exceed the budget.
_REASONING_ANSWER_HEADROOM = 8192


# A markdown heading (`# …`) or a bold lead (`**…**`) at the start of a line marks
# a new logical block in flat (OpenAI-style) reasoning, which carries no provider
# boundaries (spike tr-cy0.3.1).
_FLAT_LEAD_RE = re.compile(r"^(#{1,6}\s+\S|\*\*[^\n*]+\*\*)")


@dataclass(frozen=True)
class ReasoningPiece:
    """One reasoning fragment from a streaming delta, tagged with its segment.

    `index` is the segment ordinal: the provider's `reasoning_details` index for
    structured (Anthropic) reasoning, or None for flat (OpenAI-style) reasoning
    where the caller assigns the live ordinal. `summary` carries the provider's
    summary prose for the segment when present (Anthropic `reasoning.summary` —
    spike tr-cy0.3.1), else None. `text` is the incremental thinking prose.
    """

    text: str
    index: int | None
    summary: str | None = None


def _entry_get(entry, key):
    """Read a field off a reasoning_details entry, dict or object alike (the
    openai SDK surfaces unknown nested fields as plain dicts but tolerate both)."""
    if isinstance(entry, dict):
        return entry.get(key)
    return getattr(entry, key, None)


def _segments_from_details(details) -> list[ReasoningPiece]:
    """Per-segment pieces from Anthropic `reasoning_details` entries.

    Each entry carries an `index` (the real segment boundary). `reasoning.text`
    entries contribute thinking prose; `reasoning.summary` entries contribute the
    provider summary for that index; `reasoning.encrypted` (and any entry with
    neither text nor summary) is dropped.
    """
    pieces: list[ReasoningPiece] = []
    for entry in details:
        etype = _entry_get(entry, "type") or ""
        raw_index = _entry_get(entry, "index")
        index = int(raw_index) if raw_index is not None else 0
        if "summary" in etype:
            text, summary = "", _entry_get(entry, "summary")
        else:
            text, summary = (_entry_get(entry, "text") or ""), None
        if not text and not summary:
            continue
        pieces.append(ReasoningPiece(text=text, index=index, summary=summary))
    return pieces


def _split_flat_reasoning(text: str) -> list[str]:
    """Heuristically split flat (OpenAI-style) reasoning into logical blocks.

    The provider gives no boundaries for flat reasoning (spike tr-cy0.3.1), so a
    new block starts at a line opening with a markdown heading (`# …`) or a bold
    lead (`**…**`). Text with no such marker stays a single block. Used to
    re-segment persisted flat reasoning on reload; the live stream keeps flat as
    one ongoing segment because streamed fragments rarely align to line starts.
    """
    if not text:
        return []
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines(keepends=True):
        if current and _FLAT_LEAD_RE.match(line.lstrip()):
            blocks.append("".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("".join(current))
    return blocks or [text]


def _extract_reasoning_segments(delta) -> list[ReasoningPiece]:
    """Split a streaming delta into per-segment reasoning pieces.

    Structured (Anthropic) reasoning arrives as `delta.reasoning_details` entries
    each carrying a real `index` (segment boundary) and a `type` of
    `reasoning.text` / `reasoning.summary` / `reasoning.encrypted` — see
    `_segments_from_details`. Flat (OpenAI-style) reasoning arrives as the plain
    string `delta.reasoning` with no boundaries, surfaced as a single piece with
    `index=None`; the caller treats it as one ongoing segment. Returns `[]` when
    the delta carries no reasoning.
    """
    details = getattr(delta, "reasoning_details", None)
    if details:
        return _segments_from_details(details)
    flat = getattr(delta, "reasoning", None)
    if flat:
        return [ReasoningPiece(text=flat, index=None, summary=None)]
    return []


async def get_active_key(db: AsyncSession) -> str:
    """Return the decrypted current primary OpenRouter API key.

    Raises HTTP 503 if no primary key is active — the proxy refuses to
    forward requests rather than guessing a fallback.
    """
    row = (
        await db.execute(
            select(ApiKey).where(
                ApiKey.is_primary.is_(True),
                ApiKey.status == "active",
                ApiKey.account_blocked.isnot(True),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=503, detail="Сервис временно недоступен"
        )
    return decrypt_key(row.encrypted_key)


@dataclass(frozen=True)
class StreamChunk:
    type: Literal["content", "reasoning", "usage", "done", "tool_call"]
    delta: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Reasoning/thinking tokens reported on the final usage chunk via
    # usage.completion_tokens_details.reasoning_tokens. Already INCLUDED in
    # output_tokens; billing splits it out (BILLING_LOGIC §8). None when absent.
    reasoning_tokens: int | None = None
    # USD cost for the request, reported by OpenRouter in the final usage chunk
    # (`usage.cost`, in credits = USD). 0 for `:free` models. None when absent.
    cost_usd: float | None = None
    # OpenRouter generation id (gen-…) for this request, surfaced on streamed
    # chunks so billing can persist it and reconcile the real cost later (tr-sgz).
    openrouter_request_id: str | None = None
    # ── Reasoning segmentation (tr-cy0.3) ────────────────────────────────────
    # On a type="reasoning" chunk: True when this chunk OPENS a new reasoning
    # segment (the billing layer emits an SSE `thinking_segment` then).
    # `segment_index` is the 0-based ordinal of the segment this text belongs to;
    # `segment_summary` carries the provider's summary prose for the segment when
    # present (Anthropic reasoning.summary — spike tr-cy0.3.1), else None.
    segment_break: bool = False
    segment_index: int | None = None
    segment_summary: str | None = None
    # ── Tool calls (tr-cy0.2.4) ──────────────────────────────────────────────
    # On a type="tool_call" chunk: the streamed function-call deltas. `name`
    # arrives on the first delta of a call; `arguments_delta` carries the
    # incremental JSON-argument text; `tool_call_index` groups deltas of one call
    # within a round; `tool_call_id` is the provider's call id (for the tool reply).
    tool_call_index: int | None = None
    tool_call_id: str | None = None
    name: str | None = None
    arguments_delta: str | None = None


# Provider special/control tokens occasionally leak into the visible content
# stream (tr-d17): AI21 Jamba ends replies with ``<|eom|>``, Llama emits
# ``<|eot|>``/``<|eot_id|>``, ChatML models ``<|im_end|>``, etc. They all share
# the ``<|identifier|>`` delimiter convention with a lowercase identifier inside,
# a shape that effectively never occurs in natural prose, so a single pattern
# strips the whole family. Applied per content delta — these tokens are atomic in
# the model vocabulary and arrive whole in one chunk, so no cross-delta buffering.
_CONTROL_TOKEN_RE = re.compile(r"<\|[a-zA-Z0-9_]+\|>")


def _strip_control_tokens(text: str) -> str:
    """Remove provider control tokens (``<|eom|>``, ``<|eot|>``, …) from text."""
    return _CONTROL_TOKEN_RE.sub("", text)


def _retry_after_from_error(err: openai.APIStatusError) -> int | None:
    response = getattr(err, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class OpenRouterProxy:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def proxy_to_openrouter(
        self,
        messages: list[dict],
        model_id: str,
        stream: bool = True,
        plugins: list[dict] | None = None,
        reasoning_budget: int | None = None,
        tools: list[dict] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        api_key = await get_active_key(self.db)

        row = (
            await self.db.execute(
                select(ModelPricing).where(
                    ModelPricing.model_id == model_id,
                    ModelPricing.is_current.is_(True),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Модель не найдена")
        openrouter_model_id = row.model_id

        # Generous HTTP timeout so slow reasoning / large-context models aren't
        # cut off with a premature APITimeoutError (which the SDK raises as a
        # non-APIStatusError, so it would otherwise escape _translate and leave
        # the client spinning). The read timeout — which governs both time-to-
        # first-byte and inter-chunk silence in streaming mode — accommodates
        # both the configured default and this model's per-chunk stream ceiling.
        read_timeout = float(
            max(settings.openrouter_timeout_seconds, row.stream_timeout_seconds)
        )
        client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=OPENROUTER_BASE_URL,
            default_headers={"HTTP-Referer": settings.frontend_url},
            timeout=httpx.Timeout(
                connect=10.0, read=read_timeout, write=30.0, pool=10.0
            ),
        )

        # BUG 7: without an explicit max_tokens OpenRouter applies a per-model
        # default that truncates long answers. Allow the full configured output
        # window (mirrors the billing reservation, which reserves max_output_tokens).
        create_kwargs: dict = {
            "model": openrouter_model_id,
            "messages": messages,
            "stream": stream,
        }
        if row.max_output_tokens is not None:
            create_kwargs["max_tokens"] = row.max_output_tokens

        # OpenRouter-specific extensions travel via extra_body, which the SDK
        # merges verbatim into the JSON request body.
        #
        # `usage: {include: true}` is REQUIRED to make OpenRouter emit a final
        # usage chunk in streaming mode. Without it, streamed responses carry no
        # usage at all, so finalization silently falls back to a tiktoken estimate
        # of OUR context — which omits the page content OpenRouter injects for the
        # web-search plugin (200-300× the user's tokens), massively undercharging.
        # It also yields the real `usage.cost` (USD, incl. web-search surcharge)
        # we store as the cost basis. Requested for every request so billing always
        # finalizes on OpenRouter's reported counts (BILLING_LOGIC.md §1.1).
        extra_body: dict = {"usage": {"include": True}}
        if plugins:
            extra_body["plugins"] = plugins
        # Extended thinking: OpenRouter's unified `reasoning.max_tokens` maps to
        # Anthropic's thinking.budget_tokens (and to a budget for OpenAI-style
        # models). Anthropic requires 1024 <= budget_tokens < max_tokens.
        #
        # When thinking is NOT requested we must explicitly EXCLUDE reasoning:
        # always-on reasoning models (e.g. gpt-oss, deepseek-r1) otherwise stream
        # a thinking trace the user never asked for. `exclude` is supported by all
        # models, so it is a safe no-op for non-reasoning models.
        emit_reasoning = False
        if reasoning_budget is not None and reasoning_budget > 0:
            budget = reasoning_budget
            max_out = row.max_output_tokens
            if max_out is not None and budget >= max_out:
                # Keep the budget below the output window, leaving room for the
                # visible answer.
                budget = max_out - 1
            if budget >= _ANTHROPIC_MIN_REASONING_TOKENS:
                emit_reasoning = True
                extra_body["reasoning"] = {"max_tokens": budget}
                # Guarantee the overall max_tokens exceeds the budget. The synced
                # catalogue leaves max_output_tokens NULL for most models, so no
                # cap would otherwise be sent and the provider's default may be
                # <= budget (which makes Anthropic reject or drop the thinking).
                current_max = create_kwargs.get("max_tokens")
                if current_max is None or current_max <= budget:
                    create_kwargs["max_tokens"] = budget + _REASONING_ANSWER_HEADROOM
            # else: the model's output window is too small for thinking — fall
            # through and exclude reasoning rather than send an invalid budget.
        if not emit_reasoning:
            extra_body["reasoning"] = {"exclude": True}
        create_kwargs["extra_body"] = extra_body

        # Agentic tool-use (tr-cy0.2.4): expose the tool definitions so the model
        # can decide to call them; the engine executes them server-side and feeds
        # results back. Only sent when the caller passes tools (tools_enabled AND
        # supports_tools gate lives in the orchestrator).
        if tools:
            create_kwargs["tools"] = tools
            create_kwargs["tool_choice"] = "auto"

        # Window #1 (tr-sgz/tr-ans): the get_active_key + ModelPricing SELECTs above
        # are read-only, so end their transaction BEFORE the (possibly long) create()
        # time-to-first-byte. Left open, the connection is idle-in-transaction during
        # the TTFB, and a >60s reasoning TTFB trips Postgres's
        # idle_in_transaction_session_timeout (tr-t6o/eb95b3c), killing the stream.
        # This applies in BOTH modes now: in TOOL mode the agentic engine commits
        # each tool's flush the instant it lands (tr-ans), so there is never an
        # uncommitted artifact lingering on this shared session for an early commit
        # to prematurely persist — the concern that previously restricted this to
        # single-shot is gone. Use commit(), NOT rollback(): the orchestrator SHARES
        # this session and reads live ORM objects (pricing, reservation) at STEP 6;
        # rollback() expires them -> sync lazy-load -> MissingGreenlet. commit() ends
        # the empty read-only txn but respects expire_on_commit=False.
        await self.db.commit()

        try:
            stream_resp = await client.chat.completions.create(**create_kwargs)
        except openai.APIStatusError as err:
            raise _translate(err, openrouter_model_id) from err

        # BUG 12: count every forwarded OpenRouter request against the primary
        # key so the admin key list reflects real usage.
        await self._increment_key_usage()

        # End the transaction BEFORE entering the streaming loop. Single-shot: this
        # commits the key-usage UPDATE in its own fresh txn (the read-txn was already
        # rolled back above). Tool mode: it commits the key-usage UPDATE together with
        # any prior uncommitted tool flush. Either way the connection is merely idle
        # (not idle-in-transaction) for the whole stream, so the long-answer
        # idle_in_transaction_session_timeout kill that lost + unbilled replies can't
        # happen and finalize runs against a healthy connection (tr-sgz).
        await self.db.commit()

        try:
            usage_obj = None
            generation_id: str | None = None
            # Ordinal of the reasoning segment currently open. None until the
            # first reasoning chunk; a change opens a new segment (segment_break).
            current_segment_index: int | None = None
            # OpenRouter sends ": OPENROUTER PROCESSING" keep-alive SSE comments to
            # hold the connection open on slow models. The openai SDK's SSEDecoder
            # follows the SSE spec and silently drops comment lines (those starting
            # with ":"), so they never surface here as chunks — no handling needed.
            async for chunk in stream_resp:
                if generation_id is None:
                    chunk_id = getattr(chunk, "id", None)
                    if chunk_id:
                        generation_id = chunk_id
                if getattr(chunk, "choices", None):
                    delta = chunk.choices[0].delta
                    # Extended-thinking text arrives ahead of the visible answer,
                    # on the flat `delta.reasoning` (OpenAI-style) OR the
                    # structured `delta.reasoning_details` (Anthropic). Surface it
                    # as a separate chunk type ONLY when thinking was requested —
                    # otherwise always-on reasoning models would leak a trace.
                    if emit_reasoning:
                        for piece in _extract_reasoning_segments(delta):
                            # Structured reasoning carries the provider index as
                            # the ordinal; flat reasoning (index None) is one
                            # ongoing segment. A change of ordinal opens a new
                            # segment.
                            seg_index = (
                                piece.index
                                if piece.index is not None
                                else (current_segment_index or 0)
                            )
                            is_break = seg_index != current_segment_index
                            if is_break:
                                current_segment_index = seg_index
                            yield StreamChunk(
                                type="reasoning",
                                delta=piece.text or None,
                                segment_break=is_break,
                                segment_index=seg_index,
                                segment_summary=piece.summary,
                                openrouter_request_id=generation_id,
                            )
                    content = getattr(delta, "content", None)
                    if content:
                        # Strip leaked provider control tokens (tr-d17). Skip the
                        # chunk if it was nothing but a control token, so an empty
                        # content delta never surfaces.
                        content = _strip_control_tokens(content)
                        if content:
                            yield StreamChunk(
                                type="content",
                                delta=content,
                                openrouter_request_id=generation_id,
                            )
                    # Streamed tool calls: surface each delta.tool_calls[] entry so
                    # the orchestrator can stream live argument text and execute the
                    # call when the round ends. `name`/`id` arrive on the first delta
                    # of a call; `arguments` is incremental JSON text.
                    tool_calls = getattr(delta, "tool_calls", None)
                    if tool_calls:
                        for tc in tool_calls:
                            fn = getattr(tc, "function", None)
                            yield StreamChunk(
                                type="tool_call",
                                tool_call_index=getattr(tc, "index", 0) or 0,
                                tool_call_id=getattr(tc, "id", None),
                                name=getattr(fn, "name", None) if fn else None,
                                arguments_delta=(
                                    getattr(fn, "arguments", None) if fn else None
                                ),
                                openrouter_request_id=generation_id,
                            )
                if getattr(chunk, "usage", None) is not None:
                    usage_obj = chunk.usage

            if usage_obj is not None:
                # Reasoning tokens (a subset of completion_tokens) ride in
                # completion_tokens_details. Only report them when thinking was
                # requested, so an unprompted reasoning model isn't billed/shown
                # as having "thought".
                reasoning_tokens = None
                if emit_reasoning:
                    details = getattr(usage_obj, "completion_tokens_details", None)
                    reasoning_tokens = (
                        getattr(details, "reasoning_tokens", None)
                        if details is not None
                        else None
                    )
                yield StreamChunk(
                    type="usage",
                    input_tokens=getattr(usage_obj, "prompt_tokens", None),
                    output_tokens=getattr(usage_obj, "completion_tokens", None),
                    reasoning_tokens=reasoning_tokens,
                    # BUG 5: OpenRouter always reports the real USD cost here.
                    cost_usd=getattr(usage_obj, "cost", None),
                    openrouter_request_id=generation_id,
                )

            yield StreamChunk(type="done")
        except openai.APIStatusError as err:
            raise _translate(err, openrouter_model_id) from err

    async def _increment_key_usage(self) -> None:
        await self.db.execute(
            update(ApiKey)
            .where(ApiKey.is_primary.is_(True), ApiKey.status == "active")
            .values(
                total_requests=ApiKey.total_requests + 1,
                last_used_at=datetime.now(timezone.utc),
            )
        )

    async def proxy_to_openrouter_with_retry(
        self,
        messages: list[dict],
        model_id: str,
        max_retries: int = 3,
        plugins: list[dict] | None = None,
        reasoning_budget: int | None = None,
        tools: list[dict] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        """Per BILLING_LOGIC.md §7.2.

        Retries OpenRouterServerError and OpenRouterRateLimitError with
        exponential backoff (2^attempt seconds). 429 uses Retry-After when
        present. 4xx client errors and 503 (no key) propagate immediately.
        """
        for attempt in range(max_retries + 1):
            try:
                async for chunk in self.proxy_to_openrouter(
                    messages,
                    model_id,
                    stream=True,
                    plugins=plugins,
                    reasoning_budget=reasoning_budget,
                    tools=tools,
                ):
                    yield chunk
                return
            except OpenRouterServerError:
                if attempt >= max_retries:
                    raise
                await asyncio.sleep(2 ** attempt)
            except OpenRouterRateLimitError as err:
                if attempt >= max_retries:
                    raise
                wait = err.retry_after if err.retry_after is not None else 2 ** attempt
                await asyncio.sleep(wait)
            except OpenRouterClientError:
                raise


def _translate(err: openai.APIStatusError, model_id: str = "") -> Exception:
    status = getattr(err, "status_code", 0)
    if status == 429:
        return OpenRouterRateLimitError(retry_after=_retry_after_from_error(err))
    if 500 <= status < 600:
        return OpenRouterServerError()
    if status == 402 and not model_id.endswith(":free"):
        # Platform credits exhausted on a paid model — surface a clear message.
        # Free models are never gated this way, so they fall through to generic.
        return PaidModelsUnavailableError()
    return OpenRouterClientError()
