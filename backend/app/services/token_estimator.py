import uuid
from dataclasses import dataclass

import tiktoken
from fastapi import HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import calculate_token_cost_micro_rub
from app.core.exceptions import ContextTooLongError
from app.models.model_pricing import ModelPricing
from app.models.system_setting import SystemSetting
from app.models.usage_log import UsageLog

# Singleton encoder — loaded once at module import. tiktoken.get_encoding
# does a one-time download/cache on first call; subsequent calls are cheap.
_encoder = tiktoken.get_encoding("cl100k_base")

_PER_MESSAGE_OVERHEAD = 4
_REPLY_OVERHEAD = 3

# Flat per-image reservation estimate for multimodal messages (BILLING_LOGIC
# reservation is just a safety buffer; actual image tokens come back from
# OpenRouter's usage chunk at finalization). Conservative on purpose.
_IMAGE_TOKEN_ESTIMATE = 1000

# Reservation estimate for a `file` part (PDF/DOCX — text/code files are inlined
# as text upstream and never reach here). The real token count comes back from
# OpenRouter's usage chunk at finalization; this is a conservative buffer from
# the base64 payload length (~3 bytes per 4 chars, ~4 chars per token).
_FILE_BYTES_PER_TOKEN = 16
_FILE_TOKEN_FLOOR = 500

_DEFAULT_ESTIMATION_BUFFER = 1.3
_DEFAULT_RESERVATION_BUFFER = 2.0
_SETTINGS_CACHE_TTL_SECONDS = 60

_MIN_OUTPUT_TOKENS = 100
_MAX_OUTPUT_TOKENS = 8192

# FEATURE 2: when a conversation has history, over-estimate output at 2× the
# observed average (output costs more, so bias high); 500 when there's no history.
_OUTPUT_HISTORY_MULTIPLIER = 2
_DEFAULT_OUTPUT_NO_HISTORY = 500


@dataclass(frozen=True)
class CostEstimation:
    input_tokens: int
    output_tokens: int
    estimated_cost_micro_rub: int
    reservation_micro_rub: int


@dataclass(frozen=True)
class TruncationResult:
    messages: list[dict]
    was_truncated: bool
    original_token_count: int
    final_token_count: int
    dropped_count: int


def _content_tokens(content: "str | list[dict] | None") -> int:
    """Token estimate for one message's content (string or multimodal array).

    String content encodes via cl100k_base (unchanged). Array content sums the
    text parts and adds a flat _IMAGE_TOKEN_ESTIMATE per image part.
    """
    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                total += len(_encoder.encode(part.get("text") or ""))
            elif part.get("type") == "image_url":
                total += _IMAGE_TOKEN_ESTIMATE
            elif part.get("type") == "file":
                file_data = (part.get("file") or {}).get("file_data") or ""
                total += max(
                    _FILE_TOKEN_FLOOR, len(file_data) // _FILE_BYTES_PER_TOKEN
                )
        return total
    return len(_encoder.encode(content or ""))


def count_message_tokens(messages: list[dict]) -> int:
    """Approximate token count for a chat request via cl100k_base.

    Per BILLING_LOGIC.md §3.1: per-message overhead (4 tokens) + encoded
    content; plus a one-shot reply overhead (3 tokens). Multimodal array
    content is handled by _content_tokens (text + flat per-image estimate).
    """
    total = 0
    for msg in messages:
        total += _PER_MESSAGE_OVERHEAD
        total += _content_tokens(msg.get("content"))
    total += _REPLY_OVERHEAD
    return total


async def _get_cached_float_setting(
    db: AsyncSession, redis: Redis, key: str, default: float
) -> float:
    cache_key = f"settings:{key}"
    cached = await redis.get(cache_key)
    if cached is not None:
        return float(cached)

    raw = (
        await db.execute(
            select(SystemSetting.value).where(SystemSetting.key == key)
        )
    ).scalar_one_or_none()
    value = float(raw) if raw is not None else default
    try:
        await redis.setex(cache_key, _SETTINGS_CACHE_TTL_SECONDS, str(value))
    except RedisError:
        pass  # cache is an optimization over the DB read — fail open (tr-5rd7)
    return value


async def _get_cached_float_settings(
    db: AsyncSession, redis: Redis, specs: "list[tuple[str, float]]"
) -> "list[float]":
    """Batch of `_get_cached_float_setting`: pipeline the Redis cache reads into
    ONE round-trip (audit M5) instead of one per setting. A per-key cache miss
    falls back to a DB read + setex, exactly as the single-key path (rare; 60s
    TTL). Returned values are in `specs` order; each equals what
    `_get_cached_float_setting(key, default)` would return."""
    pipe = redis.pipeline(transaction=False)
    for key, _default in specs:
        pipe.get(f"settings:{key}")
    cached = await pipe.execute()

    out: list[float] = []
    for (key, default), raw in zip(specs, cached):
        if raw is not None:
            out.append(float(raw))
            continue
        db_raw = (
            await db.execute(
                select(SystemSetting.value).where(SystemSetting.key == key)
            )
        ).scalar_one_or_none()
        value = float(db_raw) if db_raw is not None else default
        try:
            await redis.setex(f"settings:{key}", _SETTINGS_CACHE_TTL_SECONDS, str(value))
        except RedisError:
            pass  # fail open — DB value already computed (tr-5rd7)
        out.append(value)
    return out


async def estimate_request_cost(
    db: AsyncSession,
    redis: Redis,
    messages: list[dict],
    model_id: str,
    max_output_tokens: int | None = None,
) -> CostEstimation:
    raw_input = count_message_tokens(messages)
    # M5: pipeline both settings cache reads into ONE round-trip. Fetching the
    # reservation multiplier up here (before the pricing query) is harmless on the
    # rare model-not-found path — only the success-path result matters, unchanged.
    estimation_buffer, reservation_multiplier = await _get_cached_float_settings(
        db,
        redis,
        [
            ("estimation_buffer", _DEFAULT_ESTIMATION_BUFFER),
            ("reservation_buffer_multiplier", _DEFAULT_RESERVATION_BUFFER),
        ],
    )
    input_tokens = int(raw_input * estimation_buffer)

    cap = (
        min(max_output_tokens, _MAX_OUTPUT_TOKENS)
        if max_output_tokens is not None
        else _MAX_OUTPUT_TOKENS
    )
    raw_output = int(input_tokens * 0.5)
    output_tokens = max(min(raw_output, cap), _MIN_OUTPUT_TOKENS)

    pricing_row = (
        await db.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == model_id,
                ModelPricing.is_current.is_(True),
            )
        )
    ).scalar_one_or_none()
    if pricing_row is None:
        raise HTTPException(status_code=404, detail="Модель не найдена")

    # Estimate against the same RUB price columns finalization charges from
    # (billing_service.finalize), NOT a recompute from the USD cost columns.
    # The stored RUB columns are the source of truth for what the user pays:
    # they honor admin price overrides and free models (USD=0 but a non-zero
    # RUB price), neither of which the USD recompute reflects.
    input_cost = calculate_token_cost_micro_rub(
        input_tokens, pricing_row.input_price_micro_rub
    )
    output_cost = calculate_token_cost_micro_rub(
        output_tokens, pricing_row.output_price_micro_rub
    )
    estimated_cost = input_cost + output_cost

    # reservation_multiplier was fetched up front in the M5 batch above.
    reservation = int(estimated_cost * reservation_multiplier)
    if reservation < estimated_cost:
        reservation = estimated_cost  # never reserve less than the estimate

    return CostEstimation(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_micro_rub=estimated_cost,
        reservation_micro_rub=reservation,
    )


async def _price_estimation(
    db: AsyncSession,
    redis: Redis,
    input_tokens: int,
    output_tokens: int,
    model_id: str,
    pricing: ModelPricing | None = None,
) -> tuple[int, int]:
    """Return (estimated_cost_micro_rub, reservation_micro_rub) for a token split.

    Prices against the same RUB columns finalization charges from, so admin
    overrides and free models (RUB price 0) are honored. When the caller already
    holds the current pricing row (billing_service STEP 2), pass it in so the
    estimate and the debit price off the SAME row — closing the model-sync race
    where a re-query could read a newer row than the one finalize charges from
    (tr-vg4). The debit is always consistent with pricing@STEP-2; only the
    estimate/reserve could previously diverge.
    """
    pricing_row = pricing
    if pricing_row is None:
        pricing_row = (
            await db.execute(
                select(ModelPricing).where(
                    ModelPricing.model_id == model_id,
                    ModelPricing.is_current.is_(True),
                )
            )
        ).scalar_one_or_none()
    if pricing_row is None:
        raise HTTPException(status_code=404, detail="Модель не найдена")

    input_cost = calculate_token_cost_micro_rub(
        input_tokens, pricing_row.input_price_micro_rub
    )
    output_cost = calculate_token_cost_micro_rub(
        output_tokens, pricing_row.output_price_micro_rub
    )
    estimated_cost = input_cost + output_cost

    reservation_multiplier = await _get_cached_float_setting(
        db, redis, "reservation_buffer_multiplier", _DEFAULT_RESERVATION_BUFFER
    )
    reservation = max(int(estimated_cost * reservation_multiplier), estimated_cost)
    return estimated_cost, reservation


async def estimate_request_cost_with_history(
    db: AsyncSession,
    redis: Redis,
    conversation_id: "uuid.UUID | None",
    new_message: "str | list[dict]",
    model_id: str,
    max_output_tokens: int | None = None,
    pricing: ModelPricing | None = None,
) -> CostEstimation:
    """History-aware estimate (FEATURE 2).

    Rather than re-tokenizing the whole conversation, reuse the exact token
    counts OpenRouter already reported: the most recent completed/partial
    usage_log for this conversation saw the full prior context, so its
    (input + output) tokens approximate the conversation so far. We then add a
    fresh estimate for only the new user message. Output is biased high — 2× the
    conversation's average output, or 500 with no history — since it costs more.
    """
    historical_input = 0
    avg_output: float | None = None

    if conversation_id is not None:
        latest = (
            await db.execute(
                select(UsageLog.input_tokens, UsageLog.output_tokens)
                .where(
                    UsageLog.conversation_id == conversation_id,
                    UsageLog.status.in_(("completed", "partial")),
                )
                .order_by(UsageLog.created_at.desc())
                .limit(1)
            )
        ).first()
        if latest is not None:
            historical_input = int(latest.input_tokens) + int(latest.output_tokens)

        avg_output = (
            await db.execute(
                select(func.avg(UsageLog.output_tokens)).where(
                    UsageLog.conversation_id == conversation_id,
                    UsageLog.status.in_(("completed", "partial")),
                    UsageLog.output_tokens > 0,
                )
            )
        ).scalar()

    estimation_buffer = await _get_cached_float_setting(
        db, redis, "estimation_buffer", _DEFAULT_ESTIMATION_BUFFER
    )
    new_message_tokens = count_message_tokens(
        [{"role": "user", "content": new_message}]
    )
    input_tokens = historical_input + int(new_message_tokens * estimation_buffer)

    cap = (
        min(max_output_tokens, _MAX_OUTPUT_TOKENS)
        if max_output_tokens is not None
        else _MAX_OUTPUT_TOKENS
    )
    raw_output = (
        int(float(avg_output) * _OUTPUT_HISTORY_MULTIPLIER)
        if avg_output
        else _DEFAULT_OUTPUT_NO_HISTORY
    )
    output_tokens = max(min(raw_output, cap), _MIN_OUTPUT_TOKENS)

    estimated_cost, reservation = await _price_estimation(
        db, redis, input_tokens, output_tokens, model_id, pricing=pricing
    )
    return CostEstimation(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_micro_rub=estimated_cost,
        reservation_micro_rub=reservation,
    )


def truncate_context(
    messages: list[dict],
    context_window: int,
    reserve_tokens: int = 1000,
) -> TruncationResult:
    """Drop oldest history to fit the model's context window.

    Per BILLING_LOGIC.md §3.5:
      • The system message (if present at index 0) is preserved when possible.
      • The most recent message (index -1) is ALWAYS kept.
      • Older history is removed first; we never mid-truncate a message.
      • If even [last] alone exceeds the budget, raise ContextTooLongError.

    `reserve_tokens` carves headroom out of `context_window` for the model's
    response (and minor estimation slack).
    """
    original_token_count = count_message_tokens(messages)
    budget = context_window - reserve_tokens

    if original_token_count <= budget:
        return TruncationResult(
            messages=messages,
            was_truncated=False,
            original_token_count=original_token_count,
            final_token_count=original_token_count,
            dropped_count=0,
        )

    system_msg: dict | None = None
    if messages and messages[0].get("role") == "system":
        system_msg = messages[0]
        history = messages[1:-1]
    else:
        history = messages[:-1]
    last_msg = messages[-1]

    # Start with [system?] + [last] — the inviolable core.
    kept: list[dict] = []
    if system_msg is not None:
        kept.append(system_msg)
    kept.append(last_msg)
    current_tokens = count_message_tokens(kept)

    if current_tokens > budget:
        # Drop the system message to make room.
        if system_msg is not None:
            kept = [last_msg]
            current_tokens = count_message_tokens(kept)

        if current_tokens > budget:
            # The user's own message overflows the model — cannot truncate.
            raise ContextTooLongError()

        return TruncationResult(
            messages=kept,
            was_truncated=True,
            original_token_count=original_token_count,
            final_token_count=current_tokens,
            dropped_count=len(messages) - len(kept),
        )

    # Re-add history from newest to oldest while it fits. Per-message counting
    # slightly overcounts (each adds an extra reply-overhead) — that bias is
    # conservative and safe, and final_token_count is recomputed exactly below.
    insertable: list[dict] = []
    for msg in reversed(history):
        msg_tokens = count_message_tokens([msg])
        if current_tokens + msg_tokens <= budget:
            insertable.insert(0, msg)
            current_tokens += msg_tokens
        else:
            break

    result: list[dict] = []
    if system_msg is not None:
        result.append(system_msg)
    result.extend(insertable)
    result.append(last_msg)

    return TruncationResult(
        messages=result,
        was_truncated=True,
        original_token_count=original_token_count,
        final_token_count=count_message_tokens(result),
        dropped_count=len(messages) - len(result),
    )
