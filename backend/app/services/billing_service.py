import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, replace

import anyio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import tiktoken
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    MICRO_PER_UNIT,
    calculate_token_cost_micro_rub,
    micro_to_api_string,
    micro_usd_to_micro_rub,
    usd_float_to_micro_usd,
)
from app.core.database import get_session_factory
from app.core.exceptions import (
    AccountBlockedError,
    ClientDisconnectedError,
    InsufficientFundsError,
    ModelUnavailableError,
    OpenRouterClientError,
    OpenRouterError,
    OpenRouterRateLimitError,
    OpenRouterServerError,
    ServiceUnavailableError,
    TrinityError,
)
from app.models.audit_log import AdminAuditLog
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.model_pricing import ModelPricing
from app.models.reservation import Reservation
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services.billing_types import (
    Finalization,
    RequestTiming,
    SSEEvent,
    StreamResult,
    TokenUsage,
)
from app.services.branching_service import (
    lock_conversation,
    resolve_active_path,
    restore_orphaned_answer,
    set_active_leaf_to,
)
from app.services.alert_service import create_alert
from app.services.monitoring_service import record_latency_samples
from app.services.rate_limiter import check_rate_limit
from app.services.tariff_service import _get_pricing_settings
from app.services.reasoning_summary_service import summarize_segment
from app.services.token_estimator import (
    count_message_tokens,
    estimate_request_cost_with_history,
    truncate_context,
)
from app.schemas.chat import ThinkingSegmentEvent, ThinkingSummaryEvent

if TYPE_CHECKING:
    from app.services.openrouter_proxy import OpenRouterProxy, StreamChunk

logger = logging.getLogger(__name__)

# Shared tiktoken encoder for partial-stream token estimation (BILLING_LOGIC.md §5.2/§6.3).
_encoder = tiktoken.get_encoding("cl100k_base")

# BILLING_LOGIC.md §5.2: 7% overdraft tolerance is logged but never cuts off a stream.
DEFAULT_OVERDRAFT_TOLERANCE_PERCENT = 7

# BILLING_LOGIC.md §3.5: 75/25 split — 25% of context reserved for the response.
_OUTPUT_RESERVE_FRACTION = 0.25

# BILLING_LOGIC.md §5.2: re-estimate streamed token count every ~200 chars to
# keep CPU overhead negligible (tiktoken on 200 chars is sub-millisecond).
_STREAM_REESTIMATE_INTERVAL_CHARS = 200

# BILLING_LOGIC.md §4.1 — reservation = estimated_cost × 2 (safety buffer for streaming overruns).
RESERVATION_BUFFER_FACTOR = 2

# Extended thinking: effort level → reasoning token budget (OpenRouter
# reasoning.max_tokens). Clamped below each model's max_output_tokens by the
# proxy. Reasoning tokens are billed at the cheaper INPUT rate but are a SUBSET
# of completion the token estimate doesn't cover (it only reserves a small
# visible-output window), so the budget is pre-reserved separately — see
# estimate_surcharge_micro_rub.
THINKING_BUDGETS: dict[str, int] = {"low": 2048, "medium": 8192, "high": 16384}

# Web-search plugin surcharge: OpenRouter's `web` plugin charges ~$0.005 per
# result and defaults to 5 results per search round (BILLING_LOGIC.md §7),
# folding the fee into usage.cost but NOT into token counts. Reserve one default
# round up front so the surcharge isn't silently absorbed as overdraft; the real
# folded cost is billed via the cost-basis floor at finalization.
WEB_SEARCH_SURCHARGE_USD = 0.005 * 5


def content_to_text(content: "str | list[dict]") -> str:
    """Extract the plain-text portion of a (possibly multimodal) message.

    Used for history persistence and prompt-length metrics — images are never
    stored, only their accompanying text. A string passes through unchanged.
    """
    if isinstance(content, str):
        return content
    return " ".join(
        part["text"]
        for part in content
        if isinstance(part, dict)
        and part.get("type") == "text"
        and part.get("text")
    )


def compute_actual_cost_micro_rub(
    input_tokens: int,
    output_tokens: int,
    pricing: ModelPricing,
    *,
    cost_usd: float | None,
    exchange_rate_micro_rub: int,
    markup_multiplier: float,
    web_search_enabled: bool,
    reasoning_tokens: int = 0,
) -> int:
    """Final RUB charge (μ₽) for a completed/partial request.

    Normal requests bill on tokens × per-token RUB price — those prices already
    bake in (USD price × exchange rate × markup), so they're the contract users
    see in the UI.

    Extended thinking: OpenRouter reports reasoning tokens as a SUBSET of
    `output_tokens` (completion_tokens). Trinity bills them at the cheaper INPUT
    rate, so we split them out of the visible output and re-price them: the
    visible answer at the output rate, the reasoning portion at the input rate.

    The web-search plugin adds a FLAT per-request surcharge (~$0.005/search) that
    OpenRouter folds into `usage.cost` but NOT into token counts. A reasoning
    request likewise costs the operator the provider's internal-reasoning rate
    (above our input rate). So for BOTH web-search and thinking requests with a
    real reported cost we floor the charge at OpenRouter's USD cost × exchange
    rate × markup — the single cost basis that already contains tokens AND any
    surcharge. max() keeps the token-based amount as a floor too, so a stale
    price sync can never undercut it (operator's-favor, BILLING_LOGIC.md §8.3).
    """
    visible_output_tokens = max(output_tokens - reasoning_tokens, 0)
    token_cost = (
        calculate_token_cost_micro_rub(input_tokens, pricing.input_price_micro_rub)
        + calculate_token_cost_micro_rub(
            visible_output_tokens, pricing.output_price_micro_rub
        )
        + calculate_token_cost_micro_rub(reasoning_tokens, pricing.input_price_micro_rub)
    )
    uses_cost_basis = web_search_enabled or reasoning_tokens > 0
    if not (uses_cost_basis and cost_usd is not None and cost_usd > 0):
        return token_cost

    cost_basis_micro_rub = micro_usd_to_micro_rub(
        usd_float_to_micro_usd(cost_usd), exchange_rate_micro_rub
    )
    # Apply markup with the same ceiling-integer math as calculate_rub_price
    # (round() not int(), so e.g. 1.25 doesn't truncate to a smaller multiplier).
    markup_int = round(markup_multiplier * 100)
    cost_with_markup = (cost_basis_micro_rub * markup_int + 99) // 100
    return max(token_cost, cost_with_markup)


def _resolve_reasoning_budget(
    pricing: ModelPricing,
    thinking_enabled: bool,
    thinking_effort: str,
    request_id: uuid.UUID,
) -> int | None:
    """Reasoning-token budget for an extended-thinking request, or None.

    Honored only when the model advertises support; an unsupported request is
    dropped (logged), mirroring the web-search gate. Resolved before the
    reservation so the budget can be pre-reserved (estimate_surcharge_micro_rub)
    and again passed to the proxy for the actual call.
    """
    if not thinking_enabled:
        return None
    if not pricing.supports_reasoning:
        logger.warning(
            "thinking requested for model %s which does not support it; "
            "ignoring (request %s)",
            pricing.model_id,
            request_id,
        )
        return None
    return THINKING_BUDGETS.get(thinking_effort, THINKING_BUDGETS["medium"])


def estimate_surcharge_micro_rub(
    pricing: ModelPricing,
    *,
    web_search_enabled: bool,
    reasoning_budget: int | None,
    exchange_rate_micro_rub: int,
    markup_multiplier: float,
) -> int:
    """Extra μ₽ to fold into the reservation estimate for web-search and
    extended-thinking requests.

    The token-based estimate covers neither the web-search flat fee nor the
    reasoning budget, yet compute_actual_cost_micro_rub floors the actual charge
    at OpenRouter's USD cost × rate × markup for both — so without pre-reserving
    them the gap is silently absorbed as overdraft at finalization. Reserve high
    (operator's-favor); the unused hold is refunded when the reservation is
    finalized.
    """
    surcharge = 0
    # Reasoning tokens are billed at the INPUT rate; reserve the full budget at
    # that rate (the visible-output window in the token estimate doesn't cover it).
    if reasoning_budget:
        surcharge += calculate_token_cost_micro_rub(
            reasoning_budget, pricing.input_price_micro_rub
        )
    # Web search: convert the flat USD fee to μ₽ with the same ceiling+markup math
    # finalization uses (compute_actual_cost_micro_rub). Gated on model support to
    # mirror the plugin attachment — an unsupported request never triggers a search.
    if web_search_enabled and pricing.supports_web_search:
        cost_basis = micro_usd_to_micro_rub(
            usd_float_to_micro_usd(WEB_SEARCH_SURCHARGE_USD), exchange_rate_micro_rub
        )
        markup_int = round(markup_multiplier * 100)
        surcharge += (cost_basis * markup_int + 99) // 100
    return surcharge


async def credit_balance(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount_micro_rub: int,
    idempotency_key: str,
    description: str,
    payment_id: uuid.UUID | None = None,
) -> tuple[int, bool]:
    """
    Returns (new_balance_micro_rub, was_applied).
    was_applied=False means duplicate idempotency_key — no double credit.
    """
    user = (
        await db.execute(
            select(User).where(User.id == user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    current_balance = user.balance_micro_rub
    new_balance = current_balance + amount_micro_rub

    stmt = (
        pg_insert(Transaction)
        .values(
            user_id=user_id,
            type="credit",
            amount_micro_rub=amount_micro_rub,
            balance_after_micro_rub=new_balance,
            description=description,
            payment_id=str(payment_id) if payment_id is not None else None,
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )
    result = await db.execute(stmt)

    if result.rowcount == 0:
        # Duplicate idempotency_key — leave balance untouched.
        return current_balance, False

    user.balance_micro_rub = new_balance
    await db.flush()
    return new_balance, True


async def debit_balance(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount_micro_rub: int,
    idempotency_key: str,
    description: str,
    request_id: uuid.UUID | None = None,
) -> tuple[int, int, bool]:
    """
    Returns (new_balance_micro_rub, actual_debited_micro_rub, overdraft_absorbed).
    overdraft_absorbed=True means balance was insufficient and was clamped to 0.
    """
    user = (
        await db.execute(
            select(User).where(User.id == user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    available = user.balance_micro_rub

    if available <= 0:
        return 0, 0, True

    if amount_micro_rub > available:
        actual_debit = available
        overdraft_absorbed = True
    else:
        actual_debit = amount_micro_rub
        overdraft_absorbed = False

    new_balance = available - actual_debit

    stmt = (
        pg_insert(Transaction)
        .values(
            user_id=user_id,
            type="debit",
            amount_micro_rub=-actual_debit,
            balance_after_micro_rub=new_balance,
            description=description,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )
    result = await db.execute(stmt)

    if result.rowcount == 0:
        # Duplicate idempotency_key — leave balance untouched.
        return available, 0, False

    user.balance_micro_rub = new_balance
    await db.flush()
    return new_balance, actual_debit, overdraft_absorbed


async def create_reservation(
    db: AsyncSession,
    user_id: uuid.UUID,
    estimated_cost_micro_rub: int,
    model_id: str,
    conversation_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    estimated_input_tokens: int = 0,
    estimated_output_tokens: int = 0,
) -> Reservation:
    # populate_existing=True forces this locked read to OVERWRITE the in-session
    # cache. STEP 1 already loaded this user with a plain (unlocked) read, so
    # without this the FOR UPDATE row could be ignored and a concurrent request's
    # committed reserved_micro_rub bump would go unseen — letting two requests
    # both reserve against the same balance. With it, we always see the fresh,
    # committed reserved amount under the lock (BILLING_LOGIC.md §4.1).
    user = (
        await db.execute(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    available = user.balance_micro_rub - user.reserved_micro_rub
    if available < estimated_cost_micro_rub:
        raise InsufficientFundsError(
            details={
                "required": micro_to_api_string(estimated_cost_micro_rub),
                "available": micro_to_api_string(max(available, 0)),
            },
        )

    desired = estimated_cost_micro_rub * RESERVATION_BUFFER_FACTOR
    reservation_amount = min(available, desired)

    reservation = Reservation(
        user_id=user_id,
        request_id=request_id if request_id is not None else uuid.uuid4(),
        status="active",
        reserved_micro_rub=reservation_amount,
        model_id=model_id,
        estimated_input_tokens=estimated_input_tokens,
        estimated_output_tokens=estimated_output_tokens,
    )
    db.add(reservation)

    user.reserved_micro_rub += reservation_amount
    await db.flush()

    return reservation


async def finalize_reservation(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    actual_cost_micro_rub: int,
    prompt_tokens: int,
    completion_tokens: int,
    model_id: str,
    request_id: uuid.UUID | None = None,
    *,
    conversation_id: uuid.UUID | None = None,
    openrouter_request_id: str | None = None,
    openrouter_cost_micro_usd: int = 0,
    cost_pending: bool = False,
    exchange_rate_micro_rub: int = 0,
    estimated_cost_micro_rub: int | None = None,
    status: str = "completed",
    timing: "RequestTiming | None" = None,
    prompt_length_chars: int | None = None,
    web_search_enabled: bool = False,
    reasoning_tokens: int = 0,
) -> tuple[int, bool]:
    """
    Returns (actual_debited_micro_rub, overdraft_absorbed).

    Optional kwargs populate the corresponding usage_logs columns when
    called from the orchestrator. Defaults preserve the pre-Phase-5 shape
    so legacy callers (existing tests, admin tooling) keep working.
    """
    reservation = (
        await db.execute(
            select(Reservation)
            .where(Reservation.id == reservation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if reservation is None:
        raise HTTPException(status_code=404, detail="Резервирование не найдено")
    if reservation.status != "active":
        raise HTTPException(status_code=400, detail="Резервирование уже завершено")

    user = (
        await db.execute(
            select(User).where(User.id == reservation.user_id).with_for_update()
        )
    ).scalar_one()

    # tr-6i8s: clamp the debit to what THIS request may actually draw — its own
    # held reservation plus any free balance — NEVER the funds still held for
    # OTHER active reservations. Charging into those drops balance below the
    # still-reserved total and trips chk_available_balance (balance >= reserved),
    # which rolls the whole debit back: the answer is delivered but never billed
    # (lost revenue), the reservation hangs, and a spurious critical alert fires.
    # The unextractable surplus is recorded as absorbed overdraft. (With no other
    # reservations, other_reserved == 0 and this reduces to the old balance clamp.)
    reserved_amount = reservation.reserved_micro_rub
    other_reserved = max(user.reserved_micro_rub - reserved_amount, 0)
    available_for_debit = max(user.balance_micro_rub - other_reserved, 0)
    actual_debit = min(actual_cost_micro_rub, available_for_debit)
    overdraft_absorbed = actual_debit < actual_cost_micro_rub
    overdraft_amount = actual_cost_micro_rub - actual_debit if overdraft_absorbed else 0

    new_balance = user.balance_micro_rub - actual_debit

    user.balance_micro_rub = new_balance
    user.reserved_micro_rub = other_reserved

    reservation.status = "finalized"
    reservation.actual_micro_rub = actual_debit
    reservation.refunded_micro_rub = max(reserved_amount - actual_debit, 0)
    reservation.finalized_at = datetime.now(timezone.utc)

    txn_request_id = request_id if request_id is not None else reservation.request_id

    if actual_debit > 0:
        db.add(Transaction(
            user_id=reservation.user_id,
            type="debit",
            amount_micro_rub=-actual_debit,
            balance_after_micro_rub=new_balance,
            description=f"{model_id} • {prompt_tokens + completion_tokens} tokens",
            request_id=txn_request_id,
            idempotency_key=str(reservation.request_id),
        ))

    # Pre-request estimate: orchestrator passes the real pre-buffer cost so the
    # usage_log captures the prediction we made before sending to OpenRouter.
    # Legacy callers (no estimated_cost_micro_rub) get the older approximation:
    # reserved // 2. Both are documented per BILLING_LOGIC.md §6.1.
    if estimated_cost_micro_rub is None:
        estimated_cost = reserved_amount // RESERVATION_BUFFER_FACTOR
    else:
        estimated_cost = estimated_cost_micro_rub

    db.add(UsageLog(
        user_id=reservation.user_id,
        request_id=txn_request_id,
        conversation_id=conversation_id,
        model_id=model_id,
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        estimated_input_tokens=reservation.estimated_input_tokens,
        estimated_output_tokens=reservation.estimated_output_tokens,
        estimated_cost_micro_rub=estimated_cost,
        actual_cost_micro_rub=actual_debit,
        reserved_micro_rub=reserved_amount,
        overdraft_absorbed_micro_rub=overdraft_amount,
        openrouter_cost_micro_usd=openrouter_cost_micro_usd,
        cost_pending=cost_pending,
        exchange_rate_micro_rub=exchange_rate_micro_rub,
        estimation_ms=timing.get("estimation") if timing else None,
        reservation_ms=timing.get("reservation") if timing else None,
        openrouter_ttfb_ms=timing.get("openrouter_ttfb") if timing else None,
        openrouter_total_ms=timing.get("openrouter_total") if timing else None,
        finalization_ms=timing.get("finalization") if timing else None,
        total_ms=timing.get("total") if timing else None,
        status=status,
        openrouter_request_id=openrouter_request_id,
        prompt_length_chars=prompt_length_chars,
        web_search_enabled=web_search_enabled,
        completed_at=datetime.now(timezone.utc),
    ))

    if overdraft_absorbed and overdraft_amount > 0:
        await create_alert(
            db,
            severity="critical",
            source="billing",
            title="Overdraft absorbed during finalization",
            message=(
                f"User {reservation.user_id} request {reservation.request_id}: "
                f"real cost {actual_cost_micro_rub}μ₽, charged {actual_debit}μ₽, "
                f"absorbed {overdraft_amount}μ₽."
            ),
            details={
                "user_id": str(reservation.user_id),
                "request_id": str(reservation.request_id),
                "real_cost_micro_rub": actual_cost_micro_rub,
                "charged_micro_rub": actual_debit,
                "absorbed_micro_rub": overdraft_amount,
            },
        )

    await db.flush()
    return actual_debit, overdraft_absorbed


async def release_reservation(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    failure_reason: str | None = None,
) -> None:
    reservation = (
        await db.execute(
            select(Reservation)
            .where(Reservation.id == reservation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if reservation is None:
        raise HTTPException(status_code=404, detail="Резервирование не найдено")
    if reservation.status != "active":
        raise HTTPException(status_code=400, detail="Резервирование уже завершено")

    user = (
        await db.execute(
            select(User).where(User.id == reservation.user_id).with_for_update()
        )
    ).scalar_one()

    reserved_amount = reservation.reserved_micro_rub
    user.reserved_micro_rub = max(user.reserved_micro_rub - reserved_amount, 0)

    reservation.status = "released"
    reservation.finalized_at = datetime.now(timezone.utc)

    if failure_reason is not None:
        estimated_cost = reserved_amount // RESERVATION_BUFFER_FACTOR
        db.add(UsageLog(
            user_id=reservation.user_id,
            request_id=reservation.request_id,
            model_id=reservation.model_id,
            input_tokens=0,
            output_tokens=0,
            estimated_input_tokens=reservation.estimated_input_tokens,
            estimated_output_tokens=reservation.estimated_output_tokens,
            estimated_cost_micro_rub=estimated_cost,
            actual_cost_micro_rub=0,
            reserved_micro_rub=reserved_amount,
            overdraft_absorbed_micro_rub=0,
            openrouter_cost_micro_usd=0,
            exchange_rate_micro_rub=0,
            status="failed",
            error_message=failure_reason,
        ))

    await db.flush()


async def admin_adjust_balance(
    db: AsyncSession,
    admin_id: uuid.UUID,
    user_id: uuid.UUID,
    amount_micro_rub: int,
    reason: str,
) -> tuple[int, uuid.UUID]:
    """Admin manually credits/debits a user's balance.

    Returns (new_balance_micro_rub, transaction_id).
    A debit (negative `amount_micro_rub`) that exceeds available funds
    (balance − reserved) is rejected with 422 rather than partially applied — a
    partial debit would both mislead the operator and, if it dipped into reserved
    funds, violate chk_available_balance (tr-hhj5).
    """
    if len(reason.strip()) < 5:
        raise HTTPException(
            status_code=422,
            detail="Причина должна содержать минимум 5 символов",
        )

    user = (
        await db.execute(
            select(User).where(User.id == user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    # Debits may not eat into reserved funds — that would push
    # balance_micro_rub < reserved_micro_rub and violate chk_available_balance
    # (an unhandled 500). Reject an over-available debit with 422 rather than
    # silently applying a partial amount (which misleads the operator).
    if amount_micro_rub < 0:
        available = user.balance_micro_rub - user.reserved_micro_rub
        if -amount_micro_rub > available:
            raise HTTPException(
                status_code=422,
                detail=f"Недостаточно доступных средств: доступно {micro_to_api_string(available)} ₽",
            )
    actual_delta = amount_micro_rub

    new_balance = user.balance_micro_rub + actual_delta
    user.balance_micro_rub = new_balance

    txn = Transaction(
        user_id=user_id,
        type="adjustment",
        amount_micro_rub=actual_delta,
        balance_after_micro_rub=new_balance,
        description=f"Корректировка баланса: {reason}",
        admin_user_id=admin_id,
        admin_reason=reason,
        idempotency_key=f"adj:{uuid.uuid4()}",
    )
    db.add(txn)

    admin = (
        await db.execute(select(User).where(User.id == admin_id))
    ).scalar_one()
    db.add(AdminAuditLog(
        actor_id=admin_id,
        actor_email=admin.email,
        action="admin.user.adjust_balance",
        target_type="user",
        target_id=str(user_id),
        details={
            "amount_micro_rub": amount_micro_rub,
            "actual_amount_micro_rub": actual_delta,
            "reason": reason,
            "new_balance_micro_rub": new_balance,
        },
    ))

    await db.flush()
    return new_balance, txn.id


# ──────────────────────────────────────────────────────────────────────
# Phase 5: Full billing pipeline (BILLING_LOGIC.md §2.2)
# ──────────────────────────────────────────────────────────────────────


async def _get_model_pricing_row(db: AsyncSession, model_id: str) -> ModelPricing:
    row = (
        await db.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == model_id,
                ModelPricing.is_current.is_(True),
            )
        )
    ).scalar_one_or_none()
    if row is None or row.status != "available":
        raise ModelUnavailableError()
    return row


async def get_conversation_messages(
    db: AsyncSession, conversation_id: uuid.UUID | None
) -> list[dict[str, str]]:
    """Thin re-export of message_service.get_conversation_messages.

    Kept as a module-level symbol because process_chat_request references it
    directly; the real implementation lives in message_service (Task 6.2).
    """
    from app.services.message_service import (
        get_conversation_messages as _get,
    )

    return await _get(db, conversation_id)


def estimate_actual_usage(
    context_messages: list[dict],
    collected_content: str,
    openrouter_request_id: str | None = None,
) -> TokenUsage:
    """Fallback token usage when OpenRouter doesn't emit a final usage chunk.

    Counts input via the same ChatML-aware helper used for estimation, output
    via raw tiktoken on the collected text — output has no message wrapping.
    """
    input_tokens = count_message_tokens(context_messages)
    output_tokens = len(_encoder.encode(collected_content))
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=None,
        openrouter_request_id=openrouter_request_id,
    )


async def save_messages(
    db: AsyncSession,
    conversation_id: uuid.UUID | None,
    assistant_content: str,
    actual_usage: TokenUsage,
    request_id: uuid.UUID,
    model_id: str,
    cost_micro_rub: int,
    thinking: str | None = None,
    thinking_segments: list | None = None,
    tool_steps: list | None = None,
) -> uuid.UUID | None:
    """Persist the assistant response for a finalized request.

    Returns the new assistant message's id (None for an ephemeral chat) so the
    caller can wire its branch parent + repoint the conversation's active leaf
    and stamp the streamed bubble with its real DB id.

    The user message is the endpoint's responsibility (Task 6.3): the chat-send
    endpoint persists+commits it BEFORE invoking the orchestrator so the
    conversation reflects what was asked even if the model call later fails.
    conversation_id=None means an ephemeral chat — skip persistence.

    `thinking` is the model's extended-thinking text (None when thinking was
    off); persisted so the collapsible block survives a reload.
    """
    if conversation_id is None:
        return None
    from app.services.message_service import save_assistant_message

    msg = await save_assistant_message(
        db,
        conversation_id=conversation_id,
        content=assistant_content,
        model_id=model_id,
        request_id=request_id,
        input_tokens=actual_usage.input_tokens,
        output_tokens=actual_usage.output_tokens,
        cost_micro_rub=cost_micro_rub,
        reasoning_tokens=actual_usage.reasoning_tokens or None,
        thinking=thinking,
        thinking_segments=thinking_segments,
        tool_steps=tool_steps,
    )
    return msg.id


async def _wire_assistant_branch(
    db: AsyncSession,
    conversation_id: uuid.UUID | None,
    assistant_id: uuid.UUID | None,
    parent_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Wire a just-saved assistant node onto the active branch under an EXPLICIT
    parent and establish the single contiguous active path ending at it — the
    §0.1 rule shared by send STEP-7 and the partial-disconnect path.

    The caller passes `parent_id` directly (the user turn this assistant answers):
    send → the just-saved user node, regenerate → target.parent_id, edit → the new
    user sibling. This removes the old "last active node that isn't me" derivation,
    which assumed exactly ONE in-flight assistant and mis-parented when a regenerate
    raced an edit on the same turn (two assistants wired off the same active tail).

    `set_active_leaf_to` deactivates everything else and re-activates only the
    root→assistant chain, so the §8 single-active-leaf invariant holds even if a
    concurrent mutation left a transient two-leaf state — the last writer under
    `lock_conversation` (SELECT … FOR UPDATE) wins coherently.

    `parent_id is None` falls back to the prior DB-derived parent (defensive only —
    the three real callers always pass an explicit parent).

    Returns the new active leaf id (None for an ephemeral chat).
    """
    if conversation_id is None or assistant_id is None:
        return None
    conv = await lock_conversation(db, conversation_id)
    if parent_id is None:
        active = await resolve_active_path(db, conversation_id)
        parent = next((m for m in reversed(active) if m.id != assistant_id), None)
        parent_id = parent.id if parent is not None else None
    assistant = await db.get(Message, assistant_id)
    assert assistant is not None  # just persisted in this transaction
    assistant.parent_id = parent_id
    await set_active_leaf_to(db, conv, assistant_id)
    return assistant_id


async def stream_with_cost_monitoring(
    openrouter_stream: "AsyncGenerator[StreamChunk, None]",
    reservation: Reservation,
    pricing: ModelPricing,
    overdraft_tolerance_percent: int = DEFAULT_OVERDRAFT_TOLERANCE_PERCENT,
) -> AsyncGenerator[dict, None]:
    """Wrap an OpenRouter chunk stream with running-cost monitoring.

    Yields a dict per chunk: {type: 'content'|'usage'|'done', ...}.
    Monitors a running cost estimate against the 7% overdraft ceiling
    (BILLING_LOGIC.md §5.2). The stream is NEVER cut off — exceeding the
    ceiling is logged and absorbed at finalization.

    The caller is responsible for collecting `content` deltas, capturing
    `usage` for finalization, and stopping on `done`.
    """
    collected = ""
    output_token_estimate = 0
    last_estimate_at = 0
    max_allowed_cost = int(
        reservation.reserved_micro_rub * (100 + overdraft_tolerance_percent) // 100
    )

    async for chunk in openrouter_stream:
        if chunk.type == "content" and chunk.delta:
            collected += chunk.delta
            if len(collected) - last_estimate_at >= _STREAM_REESTIMATE_INTERVAL_CHARS:
                # M3 (audit): re-encoding the full growing `collected` with
                # tiktoken every 200 chars is O(n²) and blocks the event loop on a
                # long answer. Offload the encode — the token count is identical.
                output_token_estimate = len(
                    await asyncio.to_thread(_encoder.encode, collected)
                )
                last_estimate_at = len(collected)
                running_cost = (
                    calculate_token_cost_micro_rub(
                        reservation.estimated_input_tokens,
                        pricing.input_price_micro_rub,
                    )
                    + calculate_token_cost_micro_rub(
                        output_token_estimate,
                        pricing.output_price_micro_rub,
                    )
                )
                # Log only; do not interrupt the stream (BILLING_LOGIC.md §5.3).
                _ = running_cost > max_allowed_cost
            yield {"type": "content", "delta": chunk.delta, "openrouter_request_id": chunk.openrouter_request_id}
        elif chunk.type == "reasoning" and (
            chunk.delta or chunk.segment_summary or chunk.segment_break
        ):
            # Extended-thinking text — forwarded for the SSE `thinking_delta`
            # event. Not counted toward the output-cost monitor here; the final
            # usage chunk carries the authoritative reasoning_tokens. The segment
            # boundary / provider-summary fields ride along so the orchestrator
            # can emit thinking_segment / thinking_summary (tr-cy0.3.6); a chunk
            # carrying ONLY a provider summary (delta=None) is still forwarded.
            yield {
                "type": "reasoning",
                "delta": chunk.delta,
                "openrouter_request_id": chunk.openrouter_request_id,
                "segment_break": chunk.segment_break,
                "segment_index": chunk.segment_index,
                "segment_summary": chunk.segment_summary,
            }
        elif chunk.type == "usage":
            yield {
                "type": "usage",
                "input_tokens": chunk.input_tokens or 0,
                "output_tokens": chunk.output_tokens or 0,
                "reasoning_tokens": chunk.reasoning_tokens or 0,
                "cost_usd": chunk.cost_usd,
                "openrouter_request_id": chunk.openrouter_request_id,
            }
        elif chunk.type == "done":
            yield {"type": "done"}
            return


async def finalize_partial(
    db: AsyncSession,
    reservation: Reservation,
    collected_content: str,
    context_messages: list[dict],
    pricing: ModelPricing,
    timing: RequestTiming,
    conversation_id: uuid.UUID | None = None,
    exchange_rate_micro_rub: int = 0,
    actual_usage: TokenUsage | None = None,
    prompt_length_chars: int | None = None,
    web_search_enabled: bool = False,
    markup_multiplier: float = 1.0,
    openrouter_request_id: str | None = None,
) -> Finalization:
    """Charge for what was actually delivered when a stream is cut short.

    Writes the usage_log with status='partial' (ROADMAP note) and uses the
    standard finalize path so the reservation is properly released and a
    debit transaction is created.

    If `actual_usage` is supplied (the OpenRouter usage chunk already arrived
    before the disconnect/stop), its real token counts and USD cost are used;
    otherwise we estimate from the collected text (BUG 5).
    """
    if actual_usage is not None and (
        actual_usage.input_tokens or actual_usage.output_tokens
    ):
        partial_usage = actual_usage
    else:
        partial_usage = estimate_actual_usage(context_messages, collected_content)
    actual_cost = compute_actual_cost_micro_rub(
        partial_usage.input_tokens,
        partial_usage.output_tokens,
        pricing,
        cost_usd=partial_usage.cost_usd,
        exchange_rate_micro_rub=exchange_rate_micro_rub,
        markup_multiplier=markup_multiplier,
        web_search_enabled=web_search_enabled,
        reasoning_tokens=partial_usage.reasoning_tokens,
    )

    openrouter_cost_micro_usd = (
        usd_float_to_micro_usd(partial_usage.cost_usd)
        if partial_usage.cost_usd is not None
        else 0
    )

    pre_request_estimated_cost = (
        calculate_token_cost_micro_rub(
            reservation.estimated_input_tokens, pricing.input_price_micro_rub
        )
        + calculate_token_cost_micro_rub(
            reservation.estimated_output_tokens, pricing.output_price_micro_rub
        )
    )

    actual_debited, overdraft = await finalize_reservation(
        db,
        reservation_id=reservation.id,
        actual_cost_micro_rub=actual_cost,
        prompt_tokens=partial_usage.input_tokens,
        completion_tokens=partial_usage.output_tokens,
        model_id=reservation.model_id,
        conversation_id=conversation_id,
        openrouter_request_id=openrouter_request_id or partial_usage.openrouter_request_id,
        openrouter_cost_micro_usd=openrouter_cost_micro_usd,
        exchange_rate_micro_rub=exchange_rate_micro_rub,
        estimated_cost_micro_rub=pre_request_estimated_cost,
        status="partial",
        timing=timing,
        prompt_length_chars=prompt_length_chars,
        web_search_enabled=web_search_enabled,
        reasoning_tokens=partial_usage.reasoning_tokens,
    )
    user = (
        await db.execute(
            select(User.balance_micro_rub).where(User.id == reservation.user_id)
        )
    ).scalar_one()
    return Finalization(
        actual_cost_micro_rub=actual_debited,
        balance_after_micro_rub=user,
        refunded_micro_rub=max(reservation.reserved_micro_rub - actual_debited, 0),
        overdraft_absorbed_micro_rub=(actual_cost - actual_debited) if overdraft else 0,
    )


# Subset of openrouter_proxy / network errors we surface as recoverable. Wider
# Exception is caught later as a critical error.
_OPENROUTER_ERROR_TYPES: tuple[type[Exception], ...] = (
    OpenRouterError,
    OpenRouterServerError,
    OpenRouterClientError,
    OpenRouterRateLimitError,
    ServiceUnavailableError,
)


def _sse_event(name: str, data: dict) -> SSEEvent:
    return SSEEvent(event=name, data=data)


def _segment_summary_event(seg: dict) -> SSEEvent | None:
    """Title a finished reasoning segment and emit its `thinking_summary` (once).

    The title is the FREE deterministic heuristic (no LLM, no charge — tr-cy0.3.5),
    so this never blocks the stream. Records the title on `seg` (for persistence,
    tr-cy0.3.7) and marks it titled so a segment is summarized at most once.
    Returns None when there is nothing to title.
    """
    if seg is None or seg.get("summary_emitted"):
        return None
    seg["summary_emitted"] = True
    title = summarize_segment(seg["text"], provider_summary=seg.get("provider_summary"))
    seg["summary"] = title or None
    if not title:
        return None
    return _sse_event(
        "thinking_summary",
        ThinkingSummaryEvent(segment_id=seg["id"], summary=title).model_dump(),
    )


def _coerce_running_steps(tool_steps: list[dict]) -> list[dict]:
    """Coerce any still-`running` tool step to `error` (architecture §8) so an
    interrupted step never persists as a perpetual pulse. Mutates and returns the
    list. The single place this coercion lives — used by both the normal stream end
    and the abort cleanup (the post-loop coercion never runs when the loop aborts)."""
    for st in tool_steps:
        if st.get("status") == "running":
            st["status"] = "error"
    return tool_steps


# Cap on un-persisted base64 kept inline in a tool_steps row (audit B4): a binary result
# that was NOT persisted as an Artifact (over quota) and is larger than this is dropped to
# metadata + an `unavailable` marker, instead of bloating the row's JSONB forever (re-served
# on every history load). Below the cap it stays inline so reload can still render/download.
_MAX_INLINE_RESULT_BYTES = 256 * 1024


def _persistable_results(results: list | None) -> list | None:
    """Slim rich results for persistence (Phase 4 tr-5ktl, Phase 5 tr-an0g). For a BINARY
    result (image/png or generated `file`):
      - persisted (has artifact_id) → drop the heavy base64; reload re-fetches from the
        linked artifact (no double storage);
      - un-persisted (over quota, no artifact_id) AND large → drop the base64 and mark it
        `unavailable` so the row doesn't carry tens of MB forever (B4; also fixes L1 where
        image/png was slimmed unconditionally and 404'd on reload);
      - un-persisted AND small → keep inline so reload still renders/downloads it.
    text/html and text/plain are kept inline. The LIVE SSE result keeps the full base64."""
    if not results:
        return None
    slim: list = []
    for r in results:
        if not isinstance(r, dict) or r.get("type") not in ("image/png", "file"):
            slim.append(r)
            continue
        if r.get("artifact_id"):
            slim.append({k: v for k, v in r.items() if k != "data"})  # reload from artifact
        elif isinstance(r.get("data"), str) and len(r["data"]) > _MAX_INLINE_RESULT_BYTES:
            slim.append({**{k: v for k, v in r.items() if k != "data"}, "unavailable": True})
        else:
            slim.append(r)  # small + un-persisted → keep inline for reload
    return slim


def _serialize_reasoning_segments(
    segments: list[dict], multisegment: bool
) -> list[dict] | None:
    """Project accumulated segments to the persisted ``[{id, text, summary}]``
    shape (tr-cy0.3.7). Returns None for a single segment / no reasoning so the
    row stays the legacy shape and reload falls back to ``thinking``. Titles any
    segment not yet titled (an aborted last segment) so persisted titles are
    complete — the title is the free heuristic, so this never charges or blocks.
    """
    if not multisegment:
        return None
    out: list[dict] = []
    for s in segments:
        summary = s["summary"]
        if not s.get("summary_emitted"):
            summary = (
                summarize_segment(s["text"], provider_summary=s.get("provider_summary"))
                or None
            )
        out.append({"id": s["id"], "text": s["text"], "summary": summary})
    return out


async def _finalize_or_release_on_abort(
    db: AsyncSession,
    redis: Redis,
    *,
    reservation: Reservation,
    reservation_id: uuid.UUID,
    collected_content: str,
    collected_thinking: str,
    context_messages: list[dict],
    pricing: ModelPricing,
    timing: RequestTiming,
    conversation_id: uuid.UUID | None,
    exchange_rate: int,
    markup: float,
    actual_usage: "TokenUsage | None",
    request_id: uuid.UUID,
    model_id: str,
    message_content: "str | list[dict]",
    web_search_enabled: bool,
    assistant_parent_id: uuid.UUID | None,
    failure_reason: str,
    openrouter_request_id: str | None = None,
    thinking_segments: list | None = None,
    tool_steps: list | None = None,
    created_artifact_ids: list[uuid.UUID] | None = None,
    restore_leaf_on_failure_id: uuid.UUID | None = None,
) -> None:
    """Persist a partial reply (or release the reservation) when a stream aborts.

    Shared by both abort paths — a client that closed the SSE connection
    (ClientDisconnectedError) and an in-flight task cancelled by the UI Stop /
    navigate-away that surfaces asyncio.CancelledError during stream iteration.
    Both leave the same obligation: charge for what was delivered and persist it,
    or release the reservation if nothing arrived (BILLING_LOGIC.md §7.1).

    No rollback before finalize_partial: the only DB write since the reservation
    commit is none, and a rollback would expire `reservation`, making
    finalize_partial unable to read its estimated_* fields without a sync reload.

    Always invoked through `_run_abort_cleanup`, which supplies a FRESH session
    (never the request-scoped one being torn down) and a cancellation shield so
    these DB writes commit intact — see that wrapper for the full rationale.
    """
    # Persist when there is answer text OR any tool step (tr-tc0 / A): a Stop after
    # tool work but before answer text has empty content yet real, billed tool
    # rounds — persisting (not releasing) keeps the timeline AND charges what was
    # spent. The else-branch still releases when nothing at all was delivered.
    if collected_content or tool_steps:
        # Prefer the real OpenRouter usage chunk if it already arrived before the
        # abort; otherwise finalize_partial estimates from the collected text
        # (BUG 5 — charge for what was generated).
        persisted_usage = (
            actual_usage
            if actual_usage is not None
            and (actual_usage.input_tokens or actual_usage.output_tokens)
            else estimate_actual_usage(context_messages, collected_content)
        )
        finalization = await finalize_partial(
            db,
            reservation=reservation,
            collected_content=collected_content,
            context_messages=context_messages,
            pricing=pricing,
            timing=timing,
            conversation_id=conversation_id,
            exchange_rate_micro_rub=exchange_rate,
            actual_usage=actual_usage,
            prompt_length_chars=len(content_to_text(message_content)),
            web_search_enabled=web_search_enabled,
            markup_multiplier=markup,
            openrouter_request_id=openrouter_request_id,
        )
        # Persist what was delivered so the reply survives a mid-stream abort —
        # without this the assistant message is lost and the chat looks empty on
        # reload even though the user was billed.
        assistant_id = await save_messages(
            db,
            conversation_id=conversation_id,
            assistant_content=collected_content,
            actual_usage=persisted_usage,
            request_id=request_id,
            model_id=model_id,
            cost_micro_rub=finalization.actual_cost_micro_rub,
            thinking=collected_thinking or None,
            thinking_segments=thinking_segments,
            tool_steps=tool_steps,
        )
        # §0.4: wire the partial node onto the active branch (parent + active
        # leaf) with the same §0.1 rule so it lands on the rendered path even
        # though the client is gone (no SSE event to emit).
        await _wire_assistant_branch(
            db, conversation_id, assistant_id, assistant_parent_id
        )
        # tr-ans: the artifacts created this turn were COMMITTED live by the engine
        # (one shielded commit per tool), so the request-session rollback above did
        # NOT discard them — they are visible to this fresh session. Link them by id
        # to the persisted partial message (mirrors the completed path) so the
        # «Скачать» card resolves. No re-persist — that would DUPLICATE the committed
        # rows; pruning already ran live in the executor (prune-parity).
        if assistant_id is not None and created_artifact_ids:
            from app.models.artifact import Artifact

            await db.execute(
                update(Artifact)
                .where(Artifact.id.in_(created_artifact_ids))
                .values(message_id=assistant_id)
            )
    else:
        await release_reservation(
            db, reservation_id=reservation_id, failure_reason=failure_reason
        )
        # tr-hvr1: a regenerate/edit aborted before any content already deactivated
        # the prior answer and orphaned the active leaf; restore it so the answered
        # turn isn't lost. No-op for a plain failed send (no prior answer).
        await restore_orphaned_answer(db, conversation_id, assistant_parent_id, restore_leaf_on_failure_id)
    await db.commit()
    await record_latency_samples(redis, timing)


async def _run_abort_cleanup(
    redis: Redis,
    *,
    request_db: "AsyncSession",
    reservation_id: uuid.UUID,
    collected_content: str,
    collected_thinking: str,
    context_messages: list[dict],
    pricing: ModelPricing,
    timing: RequestTiming,
    conversation_id: uuid.UUID | None,
    exchange_rate: int,
    markup: float,
    actual_usage: "TokenUsage | None",
    request_id: uuid.UUID,
    model_id: str,
    message_content: "str | list[dict]",
    web_search_enabled: bool,
    assistant_parent_id: uuid.UUID | None,
    failure_reason: str,
    openrouter_request_id: str | None = None,
    thinking_segments: list | None = None,
    tool_steps: list | None = None,
    created_artifact_ids: list[uuid.UUID] | None = None,
    restore_leaf_on_failure_id: uuid.UUID | None = None,
) -> None:
    """Finalize/release an aborted stream on a FRESH, cancellation-shielded session.

    The request-scoped `db` is unusable for abort cleanup for two reasons (tr-04a):

    1. Connection collision. On client disconnect FastAPI tears down the request's
       get_db dependency (session.close -> connection.rollback) concurrently with
       this cleanup. Sharing one asyncpg connection across both raises
       ``InterfaceError: another operation is in progress``; the connection is then
       leaked checked-out with an open transaction holding the reservation/user
       FOR UPDATE locks, so every later request needing those rows hangs until a
       30s timeout -> 503 (observed cascading onto /auth/refresh, read by the
       frontend as a logout). A session from the factory uses its own pooled
       connection, so teardown and cleanup never touch the same connection.

    2. Re-cancellation. ``asyncio.shield`` does not protect the awaiting frame
       under Starlette/anyio's level-triggered cancel scopes: the outer await is
       re-cancelled at once and the shielded work is abandoned mid-flight (proven:
       asyncio.shield -> cleanup never commits; anyio shield -> it does).
       ``anyio.CancelScope(shield=True)`` suppresses delivery for the whole block,
       so the finalize commits and the fresh session closes cleanly before control
       returns to the (still cancelled) caller.

    `reservation` stays bound to the caller's session; only its already-loaded
    scalar columns are read here (no lazy IO), while every write/lock is keyed by
    id on the fresh session.
    """
    with anyio.CancelScope(shield=True):
        # Release any residual request-session transaction/locks BEFORE the fresh
        # cleanup session touches the same rows. An in-flight round may still hold a
        # read-txn (key/pricing) or a FOR KEY SHARE lock on the conversation row; the
        # cleanup's _touch_conversation / lock_conversation (FOR UPDATE) would block
        # on it, and get_db only tears the request session down AFTER this shielded
        # block returns → a ~60s idle_in_transaction hang (tr-tc0). The artifacts this
        # turn created were already COMMITTED by the engine (one shielded commit per
        # tool, tr-ans), so this rollback does NOT discard them — they survive and are
        # linked by id on the fresh session (see _finalize_or_release_on_abort). This
        # aligns the abort path with the other error paths (timeout / OpenRouter /
        # TrinityError), which already rollback.
        #
        # `pricing` was loaded on the request session, so detach it BEFORE the
        # rollback — otherwise finalize would re-load (or fail on) its expired price
        # columns. Detaching also pins the charge to the SAME pricing row the request
        # reserved against (pricing@STEP-2), never a newer row a re-load could fetch
        # mid model-sync.
        if pricing in request_db:
            request_db.expunge(pricing)
        # Best-effort: if the request connection was already killed (idle-in-
        # transaction timeout / admin kill), its locks are already released and
        # rollback() raises — swallow it so the finalize still runs on the fresh
        # session (cause D, already-killed variant). get_db's _close_db_session
        # invalidates the dead connection on teardown.
        try:
            await request_db.rollback()
        except Exception:  # noqa: BLE001 — a dead request connection must not strand finalize
            logger.warning(
                "abort cleanup: request-session rollback failed (likely a dead "
                "connection); proceeding with finalize on the fresh session",
                exc_info=True,
            )
        async with get_session_factory()() as cleanup_db:
            # The rollback expired the request-session reservation object, so re-load
            # it by id on the fresh session (committed at STEP 4, hence visible).
            reservation = await cleanup_db.get(Reservation, reservation_id)
            if reservation is None:
                return  # reservation row gone (e.g. cron-swept) — nothing to finalize
            await _finalize_or_release_on_abort(
                cleanup_db,
                redis,
                reservation=reservation,
                reservation_id=reservation_id,
                collected_content=collected_content,
                collected_thinking=collected_thinking,
                context_messages=context_messages,
                pricing=pricing,
                timing=timing,
                conversation_id=conversation_id,
                exchange_rate=exchange_rate,
                markup=markup,
                actual_usage=actual_usage,
                request_id=request_id,
                model_id=model_id,
                message_content=message_content,
                web_search_enabled=web_search_enabled,
                assistant_parent_id=assistant_parent_id,
                failure_reason=failure_reason,
                openrouter_request_id=openrouter_request_id,
                thinking_segments=thinking_segments,
                tool_steps=tool_steps,
                created_artifact_ids=created_artifact_ids,
                restore_leaf_on_failure_id=restore_leaf_on_failure_id,
            )


@dataclass(frozen=True)
class _CompletedFinalization:
    assistant_id: uuid.UUID | None
    active_leaf_id: uuid.UUID | None
    actual_debited: int
    overdraft: bool
    balance_after: int


async def _finalize_completed_on_fresh_session(
    request_db: AsyncSession,
    redis: Redis,
    *,
    reservation_id: uuid.UUID,
    user_id: uuid.UUID,
    actual_cost: int,
    actual_usage: TokenUsage,
    pre_request_estimated_cost: int,
    openrouter_cost_micro_usd: int,
    cost_pending: bool = False,
    model_id: str,
    conversation_id: uuid.UUID | None,
    collected_content: str,
    collected_thinking: str,
    reasoning_segments: list | None,
    tool_steps: list | None,
    created_artifact_ids: list[uuid.UUID],
    assistant_parent_id: uuid.UUID | None,
    request_id: uuid.UUID,
    timing: "RequestTiming",
    exchange_rate: int,
    web_search_enabled: bool,
    prompt_length_chars: int,
    zero_token_alert: dict | None = None,
) -> _CompletedFinalization:
    """Finalize a COMPLETED stream + persist its message on a FRESH session (tr-sgz).

    The request-scoped session may have been killed mid-stream (idle-in-transaction
    timeout on a long answer, a pgbouncer/admin kill, a network blip); STEP 6–7 on it
    would then 500 at finalize and lose the already-streamed answer + never bill it.
    Like the abort path (_run_abort_cleanup, tr-04a) it finalizes on a fresh factory
    session — shielded so a late Stop at the stream-end boundary can't interrupt the
    money write — but for a COMPLETED stream: any pending request-session work is
    committed (not rolled back), then finalize + persist run on the fresh session.

    Artifacts created this turn are already COMMITTED (the engine commits each tool's
    flush per round, tr-ans) → linked by id here, NOT re-persisted (re-persist would
    duplicate the committed rows). The abort path links by id the same way (tr-ans).
    """
    async def _persist_assistant(
        fin_db: AsyncSession, cost_micro_rub: int
    ) -> "tuple[uuid.UUID | None, uuid.UUID | None]":
        assistant_id = await save_messages(
            fin_db, conversation_id=conversation_id, assistant_content=collected_content,
            actual_usage=actual_usage, request_id=request_id, model_id=model_id,
            cost_micro_rub=cost_micro_rub, thinking=collected_thinking or None,
            thinking_segments=reasoning_segments, tool_steps=tool_steps,
        )
        if assistant_id is not None and created_artifact_ids:
            from app.models.artifact import Artifact

            await fin_db.execute(
                update(Artifact)
                .where(Artifact.id.in_(created_artifact_ids))
                .values(message_id=assistant_id)
            )
        active_leaf_id = await _wire_assistant_branch(
            fin_db, conversation_id, assistant_id, assistant_parent_id
        )
        return assistant_id, active_leaf_id

    with anyio.CancelScope(shield=True):
        # Persist the request session's completed-but-uncommitted work (e.g. tool
        # artifacts) and end its transaction before the fresh session finalizes —
        # but ONLY if it actually holds one. On the normal completed path the proxy
        # already committed everything each round (reservation @STEP 4, pre-stream
        # work, every tool round), so there is nothing open and we skip — which also
        # avoids needlessly expiring the caller's loaded ORM objects. The stream
        # COMPLETED, so any pending write is legitimate and is committed (not rolled
        # back — that is the abort path's semantic). Committing here also releases the
        # request session's row locks so fin_db can lock the conversation. When the
        # connection was killed mid-stream `in_transaction` is True and the commit
        # raises on the dead connection — swallow it so finalize still runs on fin_db.
        if request_db.in_transaction():
            try:
                await request_db.commit()
            except Exception:  # noqa: BLE001 — a dead request connection must not strand finalize
                logger.warning(
                    "completed finalize: request-session commit failed (likely a dead "
                    "connection); proceeding with finalize on the fresh session",
                    exc_info=True,
                )
        async with get_session_factory()() as fin_db:
            reservation = await fin_db.get(Reservation, reservation_id)
            if reservation is None or reservation.status != "active":
                # Must-not-happen on the completed path: the reserve was committed at
                # STEP 4 and only the 5-min cron sweep flips it (refunding the reserve).
                # If it vanished/expired mid-stream the cron already refunded → issue NO
                # debit, but still persist the streamed answer so it is never lost.
                await create_alert(
                    fin_db, severity="critical", source="billing",
                    title="Reservation not finalizable at completed finalize",
                    message=(f"Reservation {reservation_id} missing/non-active at completed "
                             f"finalize for request {request_id}; answer persisted, no debit."),
                    details={"user_id": str(user_id), "request_id": str(request_id), "model_id": model_id},
                )
                assistant_id, active_leaf_id = await _persist_assistant(fin_db, 0)
                balance_after = (await fin_db.execute(
                    select(User.balance_micro_rub).where(User.id == user_id)
                )).scalar_one()
                await fin_db.commit()
                return _CompletedFinalization(assistant_id, active_leaf_id, 0, False, balance_after)

            if zero_token_alert is not None:
                await create_alert(fin_db, **zero_token_alert)

            actual_debited, overdraft = await finalize_reservation(
                fin_db, reservation_id=reservation_id, actual_cost_micro_rub=actual_cost,
                prompt_tokens=actual_usage.input_tokens, completion_tokens=actual_usage.output_tokens,
                model_id=model_id, conversation_id=conversation_id,
                openrouter_request_id=actual_usage.openrouter_request_id,
                openrouter_cost_micro_usd=openrouter_cost_micro_usd, cost_pending=cost_pending,
                exchange_rate_micro_rub=exchange_rate,
                estimated_cost_micro_rub=pre_request_estimated_cost, status="completed",
                timing=timing, prompt_length_chars=prompt_length_chars,
                web_search_enabled=web_search_enabled, reasoning_tokens=actual_usage.reasoning_tokens,
            )
            balance_after = (await fin_db.execute(
                select(User.balance_micro_rub).where(User.id == user_id)
            )).scalar_one()
            assistant_id, active_leaf_id = await _persist_assistant(fin_db, actual_debited)
            await check_low_balance_notification(fin_db, user_id, balance_after)
            await fin_db.commit()
            return _CompletedFinalization(
                assistant_id, active_leaf_id, actual_debited, overdraft, balance_after
            )


async def process_chat_request(
    user_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    message_content: "str | list[dict]",
    model_id: str,
    db: AsyncSession,
    redis: Redis,
    proxy: "OpenRouterProxy | None" = None,
    is_disconnected: "Callable[[], Awaitable[bool]] | None" = None,
    web_search_enabled: bool = False,
    thinking_enabled: bool = False,
    thinking_effort: str = "medium",
    tools_enabled: bool = False,
    excluded_image_ids: "frozenset[str] | None" = None,
    excluded_file_ids: "frozenset[str] | None" = None,
    append_user_turn: bool = True,
    assistant_parent_id: uuid.UUID | None = None,
    restore_leaf_on_failure_id: uuid.UUID | None = None,
) -> AsyncGenerator[SSEEvent, None]:
    """Full billing pipeline (BILLING_LOGIC.md §2.2 — all 7 steps).

    Yields SSE events for the chat endpoint. `proxy` is injectable so tests
    can substitute an in-memory stream without monkeypatching imports.

    `append_user_turn` (§0.3): the send path appends the new user turn to both
    the model context and the cost estimate (default True). Regenerate/edit pass
    False — their active history already ends at the user turn to answer (the
    parent user message after deactivating the target+tail, or the freshly
    inserted user sibling), so appending again would duplicate it in context and
    double-count it in the estimate.

    `assistant_parent_id` (§0.1): the EXPLICIT branch parent the new assistant
    node is wired off (the user turn it answers). Send passes the just-saved user
    node, regenerate passes the target's parent, edit passes the new user sibling.
    Passing it makes wiring race-safe (it no longer derives the parent from the
    active path, which assumed a single in-flight assistant). None falls back to
    the prior DB derivation (defensive only).
    """
    # Import here to avoid a cycle: openrouter_proxy → models that import this module.
    from app.services.openrouter_proxy import OpenRouterProxy

    request_id = uuid.uuid4()
    timing = RequestTiming()

    # STEP 1 — auth/status/rate-limit. Read-only (no FOR UPDATE): we only need
    # the status here. create_reservation (STEP 4) takes the user-row lock and
    # re-checks available balance atomically, so locking this early would just
    # hold the row across all of estimation (DB + Redis I/O), needlessly
    # serializing a single user's concurrent requests. BILLING_LOGIC.md §2.2.
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if user.status != "active":
        raise AccountBlockedError()
    await check_rate_limit(db, redis, user_id)

    # STEP 2 — pricing
    pricing = await _get_model_pricing_row(db, model_id)

    # STEP 3 — context + estimation
    timing.start("estimation")
    # Re-inject persisted images AND files into historical user messages so
    # multi-turn vision / file context works; attachments the user toggled off
    # (excluded_image_ids / excluded_file_ids) are dropped. With no stored
    # attachments this is byte-identical to the text-only history.
    from app.services.chat_image_service import build_history_with_images

    context_messages = await build_history_with_images(
        db, conversation_id, excluded_image_ids, excluded_file_ids
    )
    # Append the current message raw, then shape the WHOLE context: text/code
    # file parts are decoded and inlined as text; PDF/DOCX stay as native `file`
    # parts and require the file-parser plugin. With no file parts anywhere this
    # is byte-identical to the prior text/image-only path.
    from app.services.file_content import prepare_context_content

    # §0.3: regenerate/edit (append_user_turn=False) generate from the existing
    # active history, which already ends at the user turn to answer — appending
    # would duplicate that turn in the model context.
    if append_user_turn:
        context_messages.append({"role": "user", "content": message_content})
    # H6 (audit): per-attachment base64 decode here can block the event loop
    # ~50-100ms on large files. prepare_context_content is a pure transform, so
    # offload it to a worker thread — output is identical, the loop stays free.
    context_messages, needs_file_parser = await asyncio.to_thread(
        prepare_context_content, context_messages
    )

    truncation = truncate_context(
        context_messages,
        context_window=pricing.max_context_tokens,
        reserve_tokens=int(pricing.max_context_tokens * _OUTPUT_RESERVE_FRACTION),
    )
    context_messages = truncation.messages
    # FEATURE 2: reuse OpenRouter's exact token counts from this conversation's
    # history and estimate only the new message, rather than re-tokenizing the
    # whole transcript. §0.3: regenerate/edit (append_user_turn=False) already
    # have the user turn inside the active history (its tokens are in the prior
    # usage_log input the estimate reuses), so we pass an empty new message to
    # avoid double-counting it.
    estimation = await estimate_request_cost_with_history(
        db,
        redis,
        conversation_id,
        message_content if append_user_turn else "",
        model_id,
        pricing.max_output_tokens,
        pricing=pricing,
    )
    timing.stop("estimation")

    # Pricing settings + thinking budget are resolved BEFORE the reservation so
    # the web-search flat fee and the reasoning budget — neither of which the
    # token estimate covers — can be pre-reserved. Otherwise compute_actual_cost's
    # cost-basis floor raises the real charge above the reservation and the gap is
    # silently absorbed as overdraft at finalization.
    exchange_rate, markup = await _get_pricing_settings(db)
    reasoning_budget = _resolve_reasoning_budget(
        pricing, thinking_enabled, thinking_effort, request_id
    )
    surcharge_micro_rub = estimate_surcharge_micro_rub(
        pricing,
        web_search_enabled=web_search_enabled,
        reasoning_budget=reasoning_budget,
        exchange_rate_micro_rub=exchange_rate,
        markup_multiplier=markup,
    )

    # STEP 4 — balance check + reservation
    timing.start("reservation")
    try:
        reservation = await create_reservation(
            db,
            user_id=user_id,
            estimated_cost_micro_rub=estimation.estimated_cost_micro_rub + surcharge_micro_rub,
            model_id=model_id,
            conversation_id=conversation_id,
            request_id=request_id,
            estimated_input_tokens=estimation.input_tokens,
            estimated_output_tokens=estimation.output_tokens,
        )
    except TrinityError:
        # tr-yqmh: a PRE-stream failure (insufficient funds, rate limit) raised by
        # the reservation step happens BEFORE the streaming try/except below that
        # runs restore_orphaned_answer. For an edit/regenerate the caller already
        # deactivated the prior answer and committed it, so without restoring here
        # the turn is left orphaned (an edited question with no answer / an empty
        # regenerated slot). Revert to the prior answered turn, then re-raise so the
        # endpoint still surfaces the error. A plain send passes no restore leaf and
        # has no prior answer under the new turn, so this stays a no-op for it.
        await db.rollback()
        await restore_orphaned_answer(
            db, conversation_id, assistant_parent_id, restore_leaf_on_failure_id
        )
        await db.commit()
        raise
    # Capture the reserved amount BEFORE commit (which expires the ORM object) so
    # the agentic budget_guard (tr-rkl) can bound the loop against it.
    reserved_micro_rub = reservation.reserved_micro_rub
    await db.commit()
    # Commit so the reservation (and the user.reserved_micro_rub bump) is visible
    # to the FOR UPDATE row-locks in finalize/release, and the user-row lock taken
    # inside create_reservation is released before the (potentially long) stream.
    timing.stop("reservation")
    # Capture identity attrs so error paths (which rollback and thereby expire
    # the ORM object) can still reference them without triggering a sync reload.
    reservation_id = reservation.id

    if proxy is None:
        # DEMO_MODE: skip the real OpenRouter call so the billing pipeline
        # (reservation → finalize → debit → history) can run without an
        # OpenRouter API key. See app.services.demo_proxy.
        from app.config import settings as _settings

        if _settings.openrouter_demo_mode:
            from app.services.demo_proxy import DemoProxy

            proxy = DemoProxy(db)
        else:
            proxy = OpenRouterProxy(db)

    # OpenRouter plugins. Both the web-search and file-parser surcharges (when
    # any) are folded into the usage chunk's cost_usd, so neither needs separate
    # billing.
    plugins: list[dict] = []
    # Web-search plugin — the model decides when to actually search. Honored only
    # when the model advertises support; an unsupported request is dropped
    # (logged) rather than forwarded.
    if web_search_enabled:
        if pricing.supports_web_search:
            plugins.append({"id": "web"})
        else:
            logger.warning(
                "web_search requested for model %s which does not support it; "
                "ignoring (request %s)",
                model_id,
                request_id,
            )
    # File-parser plugin — needed only when a PDF/DOCX `file` part survived the
    # text-inlining transform. `cloudflare-ai` converts PDFs to markdown for free;
    # native-file models still receive the file directly (OpenRouter routes it).
    if needs_file_parser:
        plugins.append({"id": "file-parser", "pdf": {"engine": "cloudflare-ai"}})
    request_plugins: list[dict] | None = plugins or None

    # Extended thinking budget (reasoning_budget) was resolved before the
    # reservation so it could be pre-reserved; the proxy clamps it below the
    # model's output window. None when thinking is off or unsupported.

    collected_content = ""
    collected_thinking = ""
    # Per-segment reasoning accumulation (tr-cy0.3.6/3.7): ordered segments with
    # their text + heuristic title. `multisegment` flips True once a real
    # boundary (index ≥ 1) appears — only then are titles emitted, so a single
    # segment streams byte-identically to the legacy thinking_delta path.
    reasoning_segments: list[dict] = []
    current_seg: dict | None = None
    multisegment = False
    reasoning_finalized = False
    actual_usage: TokenUsage | None = None
    openrouter_generation_id: str | None = None
    # Agentic tool-loop (tr-cy0.2.5): active only when the user opted in AND the
    # model advertises tool support — otherwise the existing single-shot path runs
    # unchanged. `tool_steps_acc` is the live timeline persisted on finalize;
    # `created_artifact_ids` get their message_id wired after the assistant saves.
    tools_active = tools_enabled and pricing.supports_tools
    tool_steps_acc: list[dict] = []
    # tr-7br: the model can emit content in one round (a preamble), call a tool,
    # then emit more content in a later round (the answer). Those two blobs are
    # concatenated directly, so «…в файл:» + «На скриншоте…» collides into
    # «…в файл:На скриншоте…». When content RESUMES after a tool round we insert a
    # paragraph separator. This flag arms on a tool round and disarms once the
    # separator is applied to the next content delta.
    tool_round_since_content = False
    created_artifact_ids: list[uuid.UUID] = []

    def _open_segment(index: int) -> dict:
        seg = {
            "id": f"seg-{index}",
            "index": index,
            "text": "",
            "summary": None,
            "provider_summary": None,
            "summary_emitted": False,
        }
        reasoning_segments.append(seg)
        return seg

    def _emit_reasoning(seg_index, segment_break, segment_summary, delta_text):
        """Per-segment reasoning → SSE + accumulation (tr-cy0.3.6). Shared by the
        single-shot loop and the agentic-tool loop. Returns the SSE events to yield."""
        nonlocal current_seg, multisegment, collected_thinking
        out = []
        if segment_break:
            if seg_index >= 1:
                if not multisegment:
                    multisegment = True
                sev = _segment_summary_event(current_seg)
                if sev is not None:
                    out.append(sev)
            current_seg = _open_segment(seg_index)
            if seg_index >= 1:
                out.append(_sse_event(
                    "thinking_segment",
                    ThinkingSegmentEvent(
                        segment_id=current_seg["id"], index=seg_index
                    ).model_dump(),
                ))
        elif current_seg is None:
            current_seg = _open_segment(0)
        if segment_summary:
            current_seg["provider_summary"] = segment_summary
        if delta_text:
            collected_thinking += delta_text
            current_seg["text"] += delta_text
            timing.mark_first_byte_if_needed()
            out.append(_sse_event("thinking_delta", {"delta": delta_text}))
        return out

    try:
        yield _sse_event("message_start", {
            "request_id": str(request_id),
            "estimated_cost": micro_to_api_string(estimation.estimated_cost_micro_rub),
        })

        if truncation.was_truncated:
            yield _sse_event("context_truncated", {
                "original_tokens": truncation.original_token_count,
                "truncated_to_tokens": truncation.final_token_count,
                "messages_kept": len(context_messages),
                "message": "Часть истории диалога была сокращена из-за ограничений модели.",
            })

        # STEP 5 — single-shot proxy stream OR the agentic tool loop (tr-cy0.2.5).
        timing.start("openrouter_total")
        if tools_active:
            # The engine drives model↔tool rounds; we map its events to SSE,
            # accumulate the shared content/thinking + tool_steps, and SUM usage
            # across rounds. The non-tool `else` below is byte-identical to before.
            from app.schemas.chat import (
                ToolCallArgumentsDeltaEvent,
                ToolCallStartEvent,
                ToolLoopHaltedEvent,
                ToolResultEvent,
            )
            from app.services.tools import engine as _tool_engine
            from app.services.tools import registry as _tool_registry
            from app.services.tools.code_exec import register_code_exec_tool
            from app.services.tools.file_tools import ToolContext, register_builtin_tools
            from app.services.tools.prompt import AGENTIC_SYSTEM_PROMPT

            register_builtin_tools()
            # Phase 2 (tr-nyil): expose run_python alongside the file tools. Billed
            # via tokens only (fee=0, spec 2026-06-26 §1); the existing budget_guard
            # already meters the round tokens this tool's code+output consume.
            register_code_exec_tool()
            _tool_ctx = ToolContext(
                db=db, user_id=user_id, conversation_id=conversation_id,
                created_artifact_ids=created_artifact_ids,
            )
            _tools_context = [
                {"role": "system", "content": AGENTIC_SYSTEM_PROMPT}
            ] + context_messages
            _usage = {"input": 0, "output": 0, "reasoning": 0, "cost": None}

            # tr-rkl / audit M1: bound the loop's cost. Checked before each round
            # (the engine emits `halted` + stops when this returns False), so a
            # stuck loop — e.g. a failing cross-turn edit (tr-7ml) — can't burn all
            # max_rounds and silently absorb the overdraft at finalize. Priced with
            # the SAME formula finalize uses, so the bound matches the real charge.
            #
            # Ceiling = the SAME overdraft ceiling the single-shot path monitors
            # (BILLING_LOGIC.md §5.2): reserved × (100 + tolerance)/100, integer
            # math, tolerance = DEFAULT_OVERDRAFT_TOLERANCE_PERCENT (7). The reserve
            # is already 2× the estimate (§4.1), so this is a 7% grace on top of a
            # 2× buffer. Key difference from the single-shot monitor: there the
            # overdraft is only LOGGED (you can't cut one answer mid-stream), but
            # the agentic loop can decline the NEXT tool round, so here it HALTS.
            _budget_ceiling = int(
                reserved_micro_rub
                * (100 + DEFAULT_OVERDRAFT_TOLERANCE_PERCENT)
                // 100
            )

            def _budget_guard() -> bool:
                spent = compute_actual_cost_micro_rub(
                    _usage["input"],
                    _usage["output"],
                    pricing,
                    cost_usd=_usage["cost"],
                    exchange_rate_micro_rub=exchange_rate,
                    markup_multiplier=markup,
                    web_search_enabled=web_search_enabled,
                    reasoning_tokens=_usage["reasoning"],
                )
                return spent <= _budget_ceiling

            async for ev in _tool_engine.run_agentic_loop(
                proxy=proxy,
                model_id=model_id,
                context_messages=_tools_context,
                tool_ctx=_tool_ctx,
                reasoning_budget=reasoning_budget,
                tool_definitions=_tool_registry.definitions(),
                plugins=request_plugins,
                budget_guard=_budget_guard,
                # Phase 4 (tr-5ktl): feed generated PNGs back so a vision model can see
                # its own plots; gated on the model's advertised image-input capability.
                vision_capable=pricing.supports_image_input,
            ):
                if is_disconnected is not None and await is_disconnected():
                    raise ClientDisconnectedError()
                if ev.kind == "content":
                    if multisegment and not reasoning_finalized:
                        reasoning_finalized = True
                        _sev = _segment_summary_event(current_seg)
                        if _sev is not None:
                            yield _sev
                    # tr-7br: content is resuming after a tool round. If we already
                    # have content that didn't end in whitespace/newline, the two
                    # blobs would collide (…в файл:На скриншоте…). Insert a paragraph
                    # break into BOTH the persisted text AND the live stream so the
                    # reload and the live render agree. Never at the very start, and
                    # never doubled when the previous content already ends in newline.
                    if (
                        tool_round_since_content
                        and collected_content
                        and not collected_content[-1].isspace()
                    ):
                        collected_content += "\n\n"
                        yield _sse_event("content_delta", {"delta": "\n\n"})
                    tool_round_since_content = False
                    collected_content += ev.delta
                    timing.mark_first_byte_if_needed()
                    yield _sse_event("content_delta", {"delta": ev.delta})
                elif ev.kind == "reasoning":
                    for _e in _emit_reasoning(
                        ev.segment_index or 0, ev.segment_break,
                        ev.segment_summary, ev.delta,
                    ):
                        yield _e
                elif ev.kind == "tool_call_start":
                    # tr-7br: a tool round is underway; arm the separator so the next
                    # content delta (the next round's answer) won't collide with the
                    # preamble we already streamed.
                    tool_round_since_content = True
                    tool_steps_acc.append({
                        "id": ev.step_id, "tool": ev.tool, "title": ev.title,
                        "filename": None, "diff": None, "presented": False,
                        "status": "running", "output": None,
                        "stderr": None, "duration_ms": None, "truncated": False,
                        "input": None, "results": None,
                    })
                    yield _sse_event("tool_call_start", ToolCallStartEvent(
                        step_id=ev.step_id, tool=ev.tool, title=ev.title
                    ).model_dump())
                elif ev.kind == "tool_args_delta":
                    yield _sse_event(
                        "tool_call_arguments_delta",
                        ToolCallArgumentsDeltaEvent(
                            step_id=ev.step_id, delta=ev.args_delta or ""
                        ).model_dump(),
                    )
                elif ev.kind == "tool_result":
                    for _st in tool_steps_acc:
                        if _st["id"] == ev.step_id:
                            _st.update(
                                status=ev.status, filename=ev.filename,
                                diff=ev.diff, presented=bool(ev.presented),
                                output=ev.output, title=ev.title or _st["title"],
                                stderr=ev.stderr, duration_ms=ev.duration_ms,
                                truncated=ev.truncated, input=ev.input,
                                # Persist slimmed results (no base64 — images reload from
                                # their artifact); the LIVE SSE below keeps full base64.
                                results=_persistable_results(ev.results),
                            )
                            break
                    yield _sse_event("tool_result", ToolResultEvent(
                        step_id=ev.step_id, status=ev.status, filename=ev.filename,
                        diff=ev.diff, presented=bool(ev.presented), output=ev.output,
                        stderr=ev.stderr, duration_ms=ev.duration_ms,
                        truncated=ev.truncated, results=ev.results or [],
                    ).model_dump())
                elif ev.kind == "usage":
                    _u = ev.usage
                    # tr-xzy: capture the generation id (last-wins, like the single-shot
                    # path at the `evt_rid` capture below) so it reaches the usage_log.
                    _rid = _u.get("openrouter_request_id")
                    if _rid:
                        openrouter_generation_id = _rid
                    _usage["input"] += _u["input_tokens"]
                    _usage["output"] += _u["output_tokens"]
                    _usage["reasoning"] += _u["reasoning_tokens"]
                    if _u.get("cost_usd") is not None:
                        _usage["cost"] = (_usage["cost"] or 0.0) + _u["cost_usd"]
                    # Keep actual_usage current per COMPLETED round (note `_usage` is
                    # the running SUM, not the last round's chunk) so a hard Stop
                    # charges the rounds actually spent (tr-tc0 / A, operator-favor:
                    # an in-flight round whose usage never arrived stays uncharged).
                    # The post-loop build re-sets the SAME value on normal completion,
                    # so normal-path billing is byte-identical; this only takes effect
                    # when the loop exits via an abort before that build runs.
                    actual_usage = TokenUsage(
                        input_tokens=_usage["input"], output_tokens=_usage["output"],
                        reasoning_tokens=_usage["reasoning"], cost_usd=_usage["cost"],
                        openrouter_request_id=openrouter_generation_id,
                    )
                elif ev.kind == "halted":
                    yield _sse_event("tool_loop_halted", ToolLoopHaltedEvent(
                        reason=ev.reason
                    ).model_dump())
            created_artifact_ids = list(_tool_ctx.created_artifact_ids)
            actual_usage = TokenUsage(
                input_tokens=_usage["input"], output_tokens=_usage["output"],
                reasoning_tokens=_usage["reasoning"], cost_usd=_usage["cost"],
                openrouter_request_id=openrouter_generation_id,
            )
        else:
            # Per-chunk asyncio.wait_for: this ceiling applies to both TTFB and to
            # any inter-chunk silence (BILLING_LOGIC.md §7.1). The window is per-model
            # (pricing.stream_timeout_seconds) so reasoning / deep-research models get
            # several minutes. Iterating the generator manually so we can wrap
            # __anext__ in a timeout — `async for` has no per-iteration hook.
            chunk_timeout = pricing.stream_timeout_seconds
            stream_iter = stream_with_cost_monitoring(
                proxy.proxy_to_openrouter_with_retry(
                    context_messages,
                    model_id,
                    plugins=request_plugins,
                    reasoning_budget=reasoning_budget,
                ),
                reservation,
                pricing,
            ).__aiter__()
            while True:
                # Detect a client that closed the connection (navigated away or hit
                # Stop) between chunks. Raising here routes into the partial-finalize
                # branch so we bill for what was generated and persist it (BUG 1/5).
                if is_disconnected is not None and await is_disconnected():
                    raise ClientDisconnectedError()
                try:
                    evt = await asyncio.wait_for(
                        stream_iter.__anext__(),
                        timeout=chunk_timeout,
                    )
                except StopAsyncIteration:
                    break
                evt_rid = evt.get("openrouter_request_id")
                if evt_rid:
                    openrouter_generation_id = evt_rid
                if evt["type"] == "content":
                    # Reasoning is over once the answer starts: title the last open
                    # segment now, so its summary lands just before the first
                    # content_delta (the sync heuristic adds no delay).
                    if multisegment and not reasoning_finalized:
                        reasoning_finalized = True
                        sum_ev = _segment_summary_event(current_seg)
                        if sum_ev is not None:
                            yield sum_ev
                    collected_content += evt["delta"]
                    timing.mark_first_byte_if_needed()
                    yield _sse_event("content_delta", {"delta": evt["delta"]})
                elif evt["type"] == "reasoning":
                    for _e in _emit_reasoning(
                        evt.get("segment_index") or 0,
                        evt.get("segment_break"),
                        evt.get("segment_summary"),
                        evt.get("delta"),
                    ):
                        yield _e
                elif evt["type"] == "usage":
                    actual_usage = TokenUsage(
                        input_tokens=evt["input_tokens"],
                        output_tokens=evt["output_tokens"],
                        reasoning_tokens=evt.get("reasoning_tokens") or 0,
                        cost_usd=evt.get("cost_usd"),
                        openrouter_request_id=evt.get("openrouter_request_id"),
                    )
                elif evt["type"] == "done":
                    break
        # Title the last open segment if the stream ended on reasoning with no
        # answer text (the content branch would otherwise have finalized it).
        if multisegment and not reasoning_finalized:
            reasoning_finalized = True
            sum_ev = _segment_summary_event(current_seg)
            if sum_ev is not None:
                yield sum_ev
        # Interrupted tool steps never hang pulsing — persist them as errored.
        _coerce_running_steps(tool_steps_acc)
        timing.stop("openrouter_total")

        # STEP 6 — finalize (on a FRESH session; see _finalize_completed_on_fresh_session).
        timing.start("finalization")
        zero_token_alert: dict | None = None
        if actual_usage is None:
            actual_usage = estimate_actual_usage(context_messages, collected_content)
        elif actual_usage.input_tokens == 0 and actual_usage.output_tokens == 0:
            # OpenRouter occasionally emits a final usage chunk with zero tokens
            # (BILLING_LOGIC.md §7.1). Estimate from collected text and warn — the
            # alert is raised on the fresh finalize session, not the request one.
            actual_usage = estimate_actual_usage(
                context_messages,
                collected_content,
                openrouter_request_id=actual_usage.openrouter_request_id,
            )
            zero_token_alert = {
                "severity": "warning",
                "source": "openrouter",
                "title": "OpenRouter reported 0 tokens",
                "message": (
                    f"OpenRouter reported 0 tokens for request {request_id}; "
                    f"estimated from collected content instead."
                ),
                "details": {
                    "user_id": str(user_id),
                    "request_id": str(request_id),
                    "model_id": model_id,
                    "estimated_input_tokens": actual_usage.input_tokens,
                    "estimated_output_tokens": actual_usage.output_tokens,
                },
            }

        # Safety net: when actual_usage came from a fallback estimate (no usage
        # chunk, or a zero-token usage chunk), it won't carry the generation id —
        # backfill it from the last id seen on the stream so cost reconciliation
        # has it (tr-sgz). The usage-chunk path already sets it at construction.
        if actual_usage.openrouter_request_id is None and openrouter_generation_id:
            actual_usage = replace(
                actual_usage, openrouter_request_id=openrouter_generation_id
            )

        # Empty generation: the stream "completed" but delivered NOTHING — no
        # answer text, no reasoning, and zero output tokens (an empty/rejected
        # generation, e.g. a provider that accepted the prompt then returned
        # nothing). Billing it would charge full input for no output. Per
        # BILLING_LOGIC §6.3 ("no content delivered -> release"), release the hold
        # without charging and surface an error instead of a debit (tr-34mj).
        if (
            not collected_content.strip()
            and not (collected_thinking or "").strip()
            and actual_usage.output_tokens == 0
        ):
            timing.stop("finalization")
            await db.rollback()
            await release_reservation(
                db, reservation_id=reservation_id, failure_reason="empty_completion"
            )
            await restore_orphaned_answer(
                db, conversation_id, assistant_parent_id, restore_leaf_on_failure_id
            )
            await db.commit()
            yield _sse_event(
                "error",
                {
                    "code": "empty_response",
                    "message": "Модель не вернула ответ. Средства не списаны.",
                },
            )
            return

        # Read everything off the request-session ORM objects (pricing/reservation)
        # BEFORE the helper rolls the request session back — a rollback expires them.
        actual_cost = compute_actual_cost_micro_rub(
            actual_usage.input_tokens,
            actual_usage.output_tokens,
            pricing,
            cost_usd=actual_usage.cost_usd,
            exchange_rate_micro_rub=exchange_rate,
            markup_multiplier=markup,
            web_search_enabled=web_search_enabled,
            reasoning_tokens=actual_usage.reasoning_tokens,
        )

        pre_request_estimated_cost = (
            calculate_token_cost_micro_rub(
                reservation.estimated_input_tokens, pricing.input_price_micro_rub
            )
            + calculate_token_cost_micro_rub(
                reservation.estimated_output_tokens, pricing.output_price_micro_rub
            )
        )

        # cost_usd is None when OpenRouter omitted `usage.cost` from the stream
        # (vs 0.0 = an explicit free/zero cost). Flag the row cost_pending so the
        # backfill cron later fetches the real cost and the dashboard margin does
        # not treat "unknown" as a genuine $0 (tr-34mj).
        cost_known = actual_usage.cost_usd is not None
        openrouter_cost_micro_usd = (
            usd_float_to_micro_usd(actual_usage.cost_usd) if cost_known else 0
        )

        # Finalize + persist on a FRESH session so a request connection killed mid-
        # stream can't strand the charge or lose the streamed answer (tr-sgz). STEP 7
        # (save message, link artifacts, wire branch, low-balance) lives inside.
        completed = await _finalize_completed_on_fresh_session(
            db,
            redis,
            reservation_id=reservation_id,
            user_id=user_id,
            actual_cost=actual_cost,
            actual_usage=actual_usage,
            pre_request_estimated_cost=pre_request_estimated_cost,
            openrouter_cost_micro_usd=openrouter_cost_micro_usd,
            cost_pending=not cost_known,
            model_id=model_id,
            conversation_id=conversation_id,
            collected_content=collected_content,
            collected_thinking=collected_thinking,
            reasoning_segments=_serialize_reasoning_segments(
                reasoning_segments, multisegment
            ),
            tool_steps=tool_steps_acc or None,
            created_artifact_ids=created_artifact_ids,
            assistant_parent_id=assistant_parent_id,
            request_id=request_id,
            timing=timing,
            exchange_rate=exchange_rate,
            web_search_enabled=web_search_enabled,
            prompt_length_chars=len(content_to_text(message_content)),
            zero_token_alert=zero_token_alert,
        )
        timing.stop("finalization")

        # Record per-stage latency for the admin monitoring view (FEATURE 6).
        await record_latency_samples(redis, timing)

        yield _sse_event("message_done", {
            "request_id": str(request_id),
            "message_id": (
                str(completed.assistant_id) if completed.assistant_id is not None else None
            ),
            # This turn's user node (the assistant's parent) so the client can
            # swap its local placeholder for the real DB id — without it, editing
            # or resending a just-sent message targets a non-existent id (404).
            "user_message_id": (
                str(assistant_parent_id) if assistant_parent_id is not None else None
            ),
            "active_leaf_id": (
                str(completed.active_leaf_id) if completed.active_leaf_id is not None else None
            ),
            "actual_cost": micro_to_api_string(completed.actual_debited),
            "balance_after": micro_to_api_string(completed.balance_after),
            "tokens_used": {
                "input": actual_usage.input_tokens,
                "output": actual_usage.output_tokens,
                "reasoning": actual_usage.reasoning_tokens,
            },
            "overdraft_absorbed": completed.overdraft,
        })

    except asyncio.TimeoutError:
        # Per-chunk wait_for tripped: either TTFB or extended mid-stream silence
        # (BILLING_LOGIC.md §7.1). User is told no money was taken.
        await db.rollback()
        await release_reservation(
            db, reservation_id=reservation_id, failure_reason="openrouter_timeout"
        )
        await create_alert(
            db,
            severity="warning",
            source="openrouter",
            title="OpenRouter timeout",
            message=f"Request {request_id} exceeded {chunk_timeout}s without completing.",
            details={
                "user_id": str(user_id),
                "request_id": str(request_id),
                "model_id": model_id,
                "timeout_seconds": chunk_timeout,
            },
        )
        await restore_orphaned_answer(db, conversation_id, assistant_parent_id, restore_leaf_on_failure_id)
        await db.commit()
        yield _sse_event("error", {
            "code": "timeout",
            "message": f"Модель не ответила за {chunk_timeout} секунд. Средства не списаны.",
        })

    except ClientDisconnectedError:
        # §7.1: charge for what was delivered; release if nothing was delivered.
        # No SSE event — the client is gone — but the DB must reflect reality.
        # Runs on a fresh, shielded session: this disconnect also triggers FastAPI's
        # get_db teardown, and a concurrent task cancellation can land mid-cleanup —
        # sharing the request connection across either poisons it (tr-04a).
        await _run_abort_cleanup(
            redis,
            request_db=db,
            reservation_id=reservation_id,
            collected_content=collected_content,
            collected_thinking=collected_thinking,
            context_messages=context_messages,
            pricing=pricing,
            timing=timing,
            conversation_id=conversation_id,
            exchange_rate=exchange_rate,
            markup=markup,
            actual_usage=actual_usage,
            request_id=request_id,
            model_id=model_id,
            message_content=message_content,
            web_search_enabled=web_search_enabled,
            assistant_parent_id=assistant_parent_id,
            failure_reason="client_disconnected",
            openrouter_request_id=openrouter_generation_id,
            thinking_segments=_serialize_reasoning_segments(
                reasoning_segments, multisegment
            ),
            tool_steps=_coerce_running_steps(tool_steps_acc) or None,
            created_artifact_ids=created_artifact_ids,
            restore_leaf_on_failure_id=restore_leaf_on_failure_id,
        )

    except asyncio.CancelledError:
        # The in-flight generator task was cancelled (UI Stop / navigate-away on
        # a fast-streaming model): asyncio raises CancelledError — a
        # BaseException — out of `await stream_iter.__anext__()`, which neither
        # except Exception nor any handler above catches. Without this the
        # reservation is left active (cron-swept later) and the partial reply is
        # lost. Run the same finalize/release cleanup the disconnect path uses on a
        # fresh, anyio-shielded session (asyncio.shield does NOT survive Starlette's
        # level-triggered cancel scopes — see _run_abort_cleanup), then RE-RAISE — a
        # CancelledError must never be swallowed. (GeneratorExit is intentionally
        # left unhandled: awaiting during it raises RuntimeError, so its reservation
        # cleanup falls to the cron.)
        await _run_abort_cleanup(
            redis,
            request_db=db,
            reservation_id=reservation_id,
            collected_content=collected_content,
            collected_thinking=collected_thinking,
            context_messages=context_messages,
            pricing=pricing,
            timing=timing,
            conversation_id=conversation_id,
            exchange_rate=exchange_rate,
            markup=markup,
            actual_usage=actual_usage,
            request_id=request_id,
            model_id=model_id,
            message_content=message_content,
            web_search_enabled=web_search_enabled,
            assistant_parent_id=assistant_parent_id,
            failure_reason="client_cancelled",
            openrouter_request_id=openrouter_generation_id,
            thinking_segments=_serialize_reasoning_segments(
                reasoning_segments, multisegment
            ),
            tool_steps=_coerce_running_steps(tool_steps_acc) or None,
            created_artifact_ids=created_artifact_ids,
            restore_leaf_on_failure_id=restore_leaf_on_failure_id,
        )
        raise

    except _OPENROUTER_ERROR_TYPES as exc:
        await db.rollback()
        await release_reservation(
            db, reservation_id=reservation_id, failure_reason=str(exc) or exc.__class__.__name__
        )
        await create_alert(
            db,
            severity="warning",
            source="openrouter",
            title="OpenRouter request failed",
            message=f"Request {request_id} failed: {exc.__class__.__name__}: {exc}",
            details={
                "user_id": str(user_id),
                "request_id": str(request_id),
                "model_id": model_id,
                "error_type": exc.__class__.__name__,
            },
        )
        await restore_orphaned_answer(db, conversation_id, assistant_parent_id, restore_leaf_on_failure_id)
        await db.commit()
        code = getattr(exc, "code", "openrouter_error")
        message = getattr(exc, "message", None) or "Ошибка при обработке запроса. Средства не списаны."
        yield _sse_event("error", {"code": code, "message": message})

    except TrinityError:
        # Rate limit / context-too-long / etc. fired AFTER reservation. Release
        # then re-raise so the chat endpoint translates to its HTTP status.
        await db.rollback()
        await release_reservation(
            db, reservation_id=reservation_id, failure_reason="trinity_error"
        )
        await restore_orphaned_answer(db, conversation_id, assistant_parent_id, restore_leaf_on_failure_id)
        await db.commit()
        raise

    except Exception as exc:  # noqa: BLE001 — last-resort net + alert
        await db.rollback()
        await release_reservation(
            db, reservation_id=reservation_id, failure_reason=str(exc) or "internal_error"
        )
        await create_alert(
            db,
            severity="critical",
            source="billing",
            title="Unexpected billing error",
            message=f"Request {request_id}: {exc.__class__.__name__}: {exc}",
            details={
                "user_id": str(user_id),
                "request_id": str(request_id),
                "model_id": model_id,
                "error_type": exc.__class__.__name__,
            },
        )
        await restore_orphaned_answer(db, conversation_id, assistant_parent_id, restore_leaf_on_failure_id)
        await db.commit()
        raise


async def check_low_balance_notification(
    db: AsyncSession, user_id: uuid.UUID, balance_micro_rub: int
) -> None:
    """Delegates to notification_service. BILLING_LOGIC.md §15.1.

    Kept here as a thin shim so the orchestrator (Task 6.3) and any other
    callers don't need to update imports — the policy lives in notification_service.
    """
    from app.services.notification_service import (
        check_low_balance_notification as _check,
    )

    await _check(db, user_id, balance_micro_rub)
