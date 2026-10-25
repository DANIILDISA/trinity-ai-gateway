"""Chat endpoints: conversation CRUD + the SSE message-send endpoint."""
import asyncio
import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.backpressure import backpressure
from app.core.constants import micro_to_api_string
from app.core.exceptions import DuplicateRequestError, ServiceUnavailableError, TrinityError
from app.dependencies import get_db, require_active, get_redis
from app.models.chat_file import ChatFile
from app.models.chat_image import ChatImage
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.schemas.chat import (
    ActivePathSwitchRequest,
    ArtifactResponse,
    BranchTotals,
    ConversationCreate,
    ConversationListResponse,
    ConversationRename,
    ConversationResponse,
    EditMessageRequest,
    MessageResponse,
    RegenerateRequest,
    RequestResultResponse,
    SendMessageRequest,
)
from app.services.billing_service import content_to_text, process_chat_request
from app.services.branching_service import (
    deactivate_active_tail_from,
    derive_version_info,
    heal_orphaned_active_path,
    lock_conversation,
    repoint_active_leaf,
    resolve_active_path,
    set_active_leaf_to,
)
from app.services.chat_file_service import copy_files, save_message_files
from app.services.chat_image_service import copy_images, save_message_images
from app.services.message_service import save_user_message

logger = logging.getLogger(__name__)

router = APIRouter()

# Shared preemptive limiter for the non-SSE conversation routes (tr-3hs §H2). The
# 3 SSE routes (messages / edit / regenerate) are intentionally excluded — they have
# their own _CHAT_STREAM_SEMAPHORE.
_conversation_bp = Depends(backpressure("conversation", settings.conversation_backpressure_limit))

# Cap concurrent streaming chats PER WORKER. SSE streams are long-lived (minutes
# for reasoning models) and each pins a worker slot for its whole duration; without
# a bound they can starve every other request. When full we reject immediately with
# 503 rather than queue. 50 per worker × N workers is the global ceiling.
_MAX_CONCURRENT_CHAT_STREAMS = 50
_CHAT_STREAM_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT_CHAT_STREAMS)

# Idempotency window for the duplicate-send gate (tr-n5b). A retried / multi-tab /
# programmatic duplicate of the SAME logical send (same user + client_request_id)
# is rejected for this many seconds. Wide enough to cover an HTTP retry or a fast
# double-tap, short enough that the key is gone long before a user re-types the
# same prompt deliberately.
_IDEMPOTENCY_TTL_SECONDS = 120


def _sse_line(event: str, data: dict) -> str:
    """Serialize an event/data pair into a single SSE wire frame (SPEC.md §7.3)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── Conversation CRUD ──────────────────────────────────────────────────────


def _to_message_response(
    m: Message,
    version_info: dict[UUID, tuple[int, int]] | None = None,
    web_search_by_request: dict[UUID, bool] | None = None,
    partial_by_request: dict[UUID, bool] | None = None,
) -> MessageResponse:
    """Serialize one Message into its API shape (single source of truth so the
    conversation read and the active-path switch stay in lock-step).

    version_info maps message_id → (version_index, version_count); produced by
    branching_service.derive_version_info and covering EVERY message in the
    conversation. Messages absent from the map (e.g. the active-path switch
    endpoint, which doesn't recompute it) default to a single version (1, 1).

    web_search_by_request maps request_id → web_search_enabled (from usage_logs).
    web_search is True only for assistant turns whose generation used search;
    absent from the map (user turns, partial nodes, the switch endpoint) → False,
    so the reloaded meta-row globe matches what streamed live.

    partial_by_request maps request_id → (usage_logs.status == 'partial'). stopped
    is True only for assistant turns whose generation was Stopped mid-reply; absent
    from the map (user turns, the switch endpoint) or a non-partial status → False,
    so a reloaded partial reply shows «Остановлено» while a completed one keeps
    «Готово»."""
    index, count = (version_info or {}).get(m.id, (1, 1))
    web_search = bool(
        m.request_id is not None
        and (web_search_by_request or {}).get(m.request_id, False)
    )
    stopped = bool(
        m.request_id is not None
        and (partial_by_request or {}).get(m.request_id, False)
    )
    return MessageResponse(
        id=m.id,
        role=m.role,
        content=m.content,
        model_id=m.model_id,
        input_tokens=m.input_tokens,
        output_tokens=m.output_tokens,
        reasoning_tokens=m.reasoning_tokens,
        cost_micro_rub=m.cost_micro_rub,
        request_id=m.request_id,
        thinking=m.thinking,
        thinking_segments=m.thinking_segments,
        tool_steps=m.tool_steps,
        created_at=m.created_at,
        parent_id=m.parent_id,
        version_index=index,
        version_count=count,
        is_active=m.is_active,
        web_search=web_search,
        stopped=stopped,
    )


def _to_conversation_response(
    conv: Conversation,
    messages: list[Message] | None = None,
    version_info: dict[UUID, tuple[int, int]] | None = None,
    branch_totals: BranchTotals | None = None,
    web_search_by_request: dict[UUID, bool] | None = None,
    partial_by_request: dict[UUID, bool] | None = None,
) -> ConversationResponse:
    """Serialize a conversation + optional message list.

    version_info maps message_id → (version_index, version_count) and covers
    every message in the conversation, so it works for both the active-path and
    ?include=all reads. branch_totals is the all-branch aggregate (Task 13),
    populated only by GET /conversations/{id}. web_search_by_request maps
    request_id → web_search_enabled (from usage_logs) so the per-message
    web-search globe survives a reload. partial_by_request maps request_id →
    (usage_logs.status == 'partial') so a reloaded stopped reply is marked
    «Остановлено» rather than «Готово»."""
    return ConversationResponse(
        id=conv.id,
        title=conv.title,
        model_id=conv.model_id,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        last_thinking_enabled=conv.last_thinking_enabled,
        last_thinking_effort=conv.last_thinking_effort,
        last_tools_enabled=conv.last_tools_enabled,
        last_web_search_enabled=conv.last_web_search_enabled,
        branch_totals=branch_totals,
        messages=(
            [
                _to_message_response(
                    m, version_info, web_search_by_request, partial_by_request
                )
                for m in messages
            ]
            if messages is not None
            else None
        ),
    )


def _persist_last_settings(
    conv: Conversation,
    *,
    thinking_enabled: bool,
    thinking_effort: str,
    tools_enabled: bool,
    web_search_enabled: bool,
) -> None:
    """Record the generation posture of the latest request on the conversation so
    reopening the chat restores the same toggles (migration 020 / tr-hsg.12). The
    model is persisted separately via conv.model_id."""
    conv.last_thinking_enabled = thinking_enabled
    conv.last_thinking_effort = thinking_effort
    conv.last_tools_enabled = tools_enabled
    conv.last_web_search_enabled = web_search_enabled


async def _get_owned_conversation(
    db: AsyncSession, conv_id: UUID, user_id: UUID
) -> Conversation:
    """Fetch a non-deleted conversation owned by the user, else 404.

    Cross-user access returns 404 (not 403) to avoid leaking existence —
    same posture as GitHub for private resources.
    """
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="Диалог не найден")
    return conv


async def _compute_branch_totals(
    db: AsyncSession, conversation_id: UUID
) -> BranchTotals:
    """Read the «Все ветки» totals from the denormalized conversations.bt_* columns
    (tr-9wq). The columns are maintained by the messages_branch_totals DB trigger
    (migration 018), so this is a single-row read instead of a 6-way SUM/COUNT over
    every message on each conversation GET (audit §H3)."""
    row = (
        await db.execute(
            select(
                Conversation.bt_total_cost_micro_rub,
                Conversation.bt_total_input_tokens,
                Conversation.bt_total_output_tokens,
                Conversation.bt_total_reasoning_tokens,
                Conversation.bt_message_count,
                Conversation.bt_generation_count,
            ).where(Conversation.id == conversation_id)
        )
    ).one()
    return BranchTotals(
        total_cost_micro_rub=int(row[0]),
        total_input_tokens=int(row[1]),
        total_output_tokens=int(row[2]),
        total_reasoning_tokens=int(row[3]),
        message_count=int(row[4]),
        generation_count=int(row[5]),
    )


async def _web_search_by_request(
    db: AsyncSession, messages: list[Message]
) -> dict[UUID, bool]:
    """Map request_id → web_search_enabled for the given messages, read from
    usage_logs in ONE query (web_search isn't stored on the message row, only on
    its usage_log keyed by request_id). Lets a conversation read restore the
    per-message web-search globe that otherwise exists only on the live SSE.

    Only assistant turns carry a request_id; user turns and partial nodes without
    one are skipped (they're never search-enabled anyway)."""
    from app.models.usage_log import UsageLog

    request_ids = [m.request_id for m in messages if m.request_id is not None]
    if not request_ids:
        return {}
    rows = (
        await db.execute(
            select(UsageLog.request_id, UsageLog.web_search_enabled).where(
                UsageLog.request_id.in_(request_ids)
            )
        )
    ).all()
    return {rid: bool(flag) for rid, flag in rows}


async def _partial_by_request(
    db: AsyncSession, messages: list[Message]
) -> dict[UUID, bool]:
    """Map request_id → was-stopped for the given messages, read from usage_logs
    in ONE query (the stopped/partial state isn't stored on the message row, only
    on its usage_log keyed by request_id). Lets a conversation read distinguish a
    Stopped partial reply from a completed one (tr-fo1).

    Only status == 'partial' (the user-Stop terminal state written by the tr-tc0
    finalize path) counts as stopped; every other status (completed / failed /
    refunded / active) maps to False so a non-partial reply is never mis-read as
    stopped. Only assistant turns carry a request_id; user turns are skipped."""
    from app.models.usage_log import UsageLog

    request_ids = [m.request_id for m in messages if m.request_id is not None]
    if not request_ids:
        return {}
    rows = (
        await db.execute(
            select(UsageLog.request_id, UsageLog.status).where(
                UsageLog.request_id.in_(request_ids)
            )
        )
    ).all()
    return {rid: status == "partial" for rid, status in rows}


@router.post(
    "/conversations",
    response_model=ConversationResponse,
    response_model_exclude_none=True,
    dependencies=[_conversation_bp],
)
async def create_conversation(
    body: ConversationCreate,
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> ConversationResponse:
    conv = Conversation(
        user_id=user.id,
        model_id=body.model_id,
        title=body.title,
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return _to_conversation_response(conv)


@router.get(
    "/conversations",
    response_model=ConversationListResponse,
    response_model_exclude_none=True,
    dependencies=[_conversation_bp],
)
async def list_conversations(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    archived: bool = Query(False),
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> ConversationListResponse:
    # archived=true returns the soft-deleted (archived) conversations so the
    # sidebar archive section can list them for restore / permanent delete.
    deleted_filter = (
        Conversation.deleted_at.is_not(None)
        if archived
        else Conversation.deleted_at.is_(None)
    )
    base = select(Conversation).where(
        Conversation.user_id == user.id,
        deleted_filter,
    )

    total = (
        await db.execute(
            select(func.count()).select_from(base.subquery())
        )
    ).scalar() or 0

    rows = (
        await db.execute(
            base.order_by(Conversation.updated_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).scalars().all()

    return ConversationListResponse(
        items=[_to_conversation_response(c) for c in rows],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationResponse,
    dependencies=[_conversation_bp],
)
async def get_conversation(
    conversation_id: UUID,
    include: str | None = Query(None),
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> ConversationResponse:
    """Return the conversation with its ACTIVE root→leaf path.

    By default only the active path is returned (is_active + deleted_at IS NULL,
    ordered created_at,id; derived from active_leaf_id with a created_at fallback
    for legacy rows). ?include=all additionally returns inactive siblings so the
    frontend can lazily populate the < n/m > switcher. version_index/version_count
    are derived over the WHOLE conversation, so siblings get correct counts in
    either mode. branch_totals (Task 13) always reflects every branch.
    """
    conv = await _get_owned_conversation(db, conversation_id, user.id)

    # Persistently repair a fast regenerate/edit→Stop orphan before reading, so the
    # answered turn comes back AND the DB stops being orphaned (tr-5o8r). Only on the
    # active-path read — include=all returns every sibling regardless of is_active.
    if include != "all" and await heal_orphaned_active_path(db, conv.id):
        await db.commit()
        conv = await _get_owned_conversation(db, conversation_id, user.id)

    version_info = await derive_version_info(db, conv.id)
    branch_totals = await _compute_branch_totals(db, conv.id)

    if include == "all":
        messages = list(
            (
                await db.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == conv.id,
                        Message.deleted_at.is_(None),
                    )
                    .order_by(Message.created_at.asc(), Message.id.asc())
                )
            ).scalars()
        )
    else:
        messages = await resolve_active_path(db, conv.id)

    web_search_by_request = await _web_search_by_request(db, messages)
    partial_by_request = await _partial_by_request(db, messages)

    return _to_conversation_response(
        conv,
        messages=messages,
        version_info=version_info,
        branch_totals=branch_totals,
        web_search_by_request=web_search_by_request,
        partial_by_request=partial_by_request,
    )


@router.get(
    "/conversations/{conversation_id}/requests/{request_id}",
    response_model=RequestResultResponse,
    dependencies=[_conversation_bp],
)
async def get_request_result(
    conversation_id: UUID,
    request_id: UUID,
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> RequestResultResponse:
    """Return the persisted assistant result for one request_id, message_done-shaped.

    The Stop path closes the SSE connection before the server can emit message_done,
    so the client polls this to fill in the bubble's id/cost/tokens without a full
    reload (tr-sgz). 404 until the assistant message is persisted, and 404 for a
    conversation the caller doesn't own (no existence leak)."""
    conv = await _get_owned_conversation(db, conversation_id, user.id)
    msg = (
        await db.execute(
            select(Message).where(
                Message.conversation_id == conv.id,
                Message.request_id == request_id,
                Message.role == "assistant",
                Message.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="Результат запроса не найден")

    # Derive stopped from the usage_log (status == 'partial') so the post-Stop
    # reconcile can flip the live bubble to «Остановлено» without a reload — same
    # read-path source as GET /conversations, no finalize change (tr-fo1).
    partial_map = await _partial_by_request(db, [msg])

    return RequestResultResponse(
        request_id=request_id,
        message_id=msg.id,
        user_message_id=msg.parent_id,
        active_leaf_id=conv.active_leaf_id,
        actual_cost=micro_to_api_string(msg.cost_micro_rub or 0),
        balance_after=micro_to_api_string(user.balance_micro_rub),
        tokens_used={
            "input": msg.input_tokens or 0,
            "output": msg.output_tokens or 0,
            "reasoning": msg.reasoning_tokens or 0,
        },
        thinking_segments=msg.thinking_segments,
        tool_steps=msg.tool_steps,
        stopped=bool(
            msg.request_id is not None
            and partial_map.get(msg.request_id, False)
        ),
    )


@router.get(
    "/conversations/{conversation_id}/artifact",
    response_model=ArtifactResponse,
    dependencies=[_conversation_bp],
)
async def get_artifact(
    conversation_id: UUID,
    filename: str,
    version: int | None = Query(default=None, ge=1),
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> ArtifactResponse:
    """Return a model-authored artifact for the artifact card / download / canvas
    (tr-cy0.2.13). Keyed by (conversation, filename). Default = LATEST version;
    `?version=N` selects an older one (tr-qil). The response carries the returned
    row's `version` and the group's `version_count` so the canvas can render a
    <n/m> switcher. 404 for an unknown file/version or a conversation the caller
    doesn't own (no existence leak)."""
    from app.models.artifact import Artifact

    conv = await _get_owned_conversation(db, conversation_id, user.id)
    # The latest row fixes the group_id (filename is shared across versions, but a
    # conversation could in principle reuse a name across groups — anchor on the
    # newest one, matching what the card/download already return).
    latest = (
        await db.execute(
            select(Artifact)
            .where(Artifact.conversation_id == conv.id, Artifact.filename == filename)
            .order_by(Artifact.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        raise HTTPException(status_code=404, detail="Артефакт не найден")

    version_count = (
        await db.execute(
            select(func.count())
            .select_from(Artifact)
            .where(
                Artifact.conversation_id == conv.id,
                Artifact.group_id == latest.group_id,
            )
        )
    ).scalar_one()

    if version is None or version == latest.version:
        row = latest
    else:
        row = (
            await db.execute(
                select(Artifact).where(
                    Artifact.conversation_id == conv.id,
                    Artifact.group_id == latest.group_id,
                    Artifact.version == version,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Версия артефакта не найдена")

    return ArtifactResponse(
        filename=row.filename,
        media_type=row.media_type,
        content=row.content,
        version=row.version,
        version_count=version_count,
    )


@router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationResponse,
    response_model_exclude_none=True,
    dependencies=[_conversation_bp],
)
async def rename_conversation(
    conversation_id: UUID,
    body: ConversationRename,
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> ConversationResponse:
    conv = await _get_owned_conversation(db, conversation_id, user.id)
    conv.title = body.title
    conv.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(conv)
    return _to_conversation_response(conv)


@router.delete(
    "/conversations/{conversation_id}",
    status_code=204,
    response_class=Response,
    dependencies=[_conversation_bp],
)
async def delete_conversation(
    conversation_id: UUID,
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> Response:
    conv = await _get_owned_conversation(db, conversation_id, user.id)
    now = datetime.now(timezone.utc)
    conv.deleted_at = now
    conv.updated_at = now
    await db.commit()
    return Response(status_code=204)


async def _get_archived_conversation(
    db: AsyncSession, conv_id: UUID, user_id: UUID
) -> Conversation:
    """Fetch an archived (soft-deleted) conversation owned by the user, else 404."""
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_not(None),
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="Архивный диалог не найден")
    return conv


@router.post(
    "/conversations/{conversation_id}/restore",
    response_model=ConversationResponse,
    response_model_exclude_none=True,
    dependencies=[_conversation_bp],
)
async def restore_conversation(
    conversation_id: UUID,
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> ConversationResponse:
    """Un-archive a soft-deleted conversation (clears deleted_at).

    Restored chats have deleted_at=NULL again, so the 30-day retention purge
    (which targets deleted_at older than 30 days) leaves them untouched.
    """
    conv = await _get_archived_conversation(db, conversation_id, user.id)
    conv.deleted_at = None
    conv.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(conv)
    return _to_conversation_response(conv)


@router.delete(
    "/conversations/{conversation_id}/permanent",
    status_code=204,
    response_class=Response,
    dependencies=[_conversation_bp],
)
async def permanent_delete_conversation(
    conversation_id: UUID,
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Hard-delete an already-archived conversation and its messages.

    Only archived (soft-deleted) conversations can be permanently removed — this
    prevents accidental hard-deletes of active chats.

    The conversation's stored images and files (base64 in `chat_images` /
    `chat_files`) are deleted too. This must happen BEFORE the conversation row is
    removed: both FKs are ON DELETE SET NULL, so dropping the conversation first
    would null out `conversation_id` and orphan the attachments in the Files view
    instead of removing them.
    """
    conv = await _get_archived_conversation(db, conversation_id, user.id)
    await db.execute(
        sa_delete(ChatImage).where(ChatImage.conversation_id == conv.id)
    )
    await db.execute(
        sa_delete(ChatFile).where(ChatFile.conversation_id == conv.id)
    )
    await db.execute(sa_delete(Message).where(Message.conversation_id == conv.id))
    await db.execute(sa_delete(Conversation).where(Conversation.id == conv.id))
    await db.commit()
    return Response(status_code=204)


# ── Send message (SSE) ────────────────────────────────────────────────────


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: UUID,
    body: SendMessageRequest,
    request: Request,
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> StreamingResponse:
    """Send a user message and stream the assistant response via SSE.

    Wires conversation ownership → user-message persistence → billing pipeline.
    The assistant message is persisted by process_chat_request itself
    (via save_messages) after billing finalization (Task 6.2).
    """
    conv = await _get_owned_conversation(db, conversation_id, user.id)

    # Idempotency gate (tr-n5b) — defense-in-depth behind the frontend submit
    # lock. A duplicate of the SAME logical send (multi-tab / HTTP retry /
    # programmatic replay) carries the SAME client_request_id; claim it ATOMICALLY
    # in Redis (SET NX = set-if-absent) BEFORE persisting the user message and
    # BEFORE any reservation/charge. First claim wins and proceeds exactly as
    # today; a losing duplicate is rejected with a clean 409 — NO message, NO
    # reservation, NO charge. Scoped per user so two users can never collide on a
    # shared id. client_request_id is None for older clients ⇒ no gate ⇒ unchanged
    # behavior (full backward compatibility). This sits strictly BEFORE the money
    # path; it never touches estimation/reservation/finalize math.
    if body.client_request_id is not None:
        try:
            claimed = await redis.set(
                f"idemp:{user.id}:{body.client_request_id}",
                "1",
                nx=True,
                ex=_IDEMPOTENCY_TTL_SECONDS,
            )
        except RedisError as exc:
            # The gate sits BEFORE the money path; if we cannot claim the key we must NOT
            # proceed — a retried duplicate would double-charge. Fail CLOSED with a retryable
            # 503 (covers an OOM ResponseError under noeviction, tr-5rd7), never fail-open,
            # never a raw 500.
            raise ServiceUnavailableError(
                "Сервис временно недоступен, повторите попытку", retry_after=2
            ) from exc
        if not claimed:
            raise DuplicateRequestError()

    # Per-request model override: callers may switch model mid-conversation
    # (SCHEMA.md §3.6). Persist the new default for subsequent reads.
    if body.model != conv.model_id:
        conv.model_id = body.model
        conv.updated_at = datetime.now(timezone.utc)

    # Remember this request's posture so reopening the chat restores it (tr-hsg.12).
    _persist_last_settings(
        conv,
        thinking_enabled=body.thinking_enabled,
        thinking_effort=body.thinking_effort,
        tools_enabled=body.tools_enabled,
        web_search_enabled=body.web_search_enabled,
    )

    # Multimodal content arrives as a list of typed parts; normalize to plain
    # dicts so nothing pydantic reaches the OpenAI SDK, and so the billing
    # pipeline sees a uniform str | list[dict]. Images are NEVER persisted —
    # only the text is stored in history (base64 lives solely in this request).
    structured_content = (
        body.content
        if isinstance(body.content, str)
        else [part.model_dump() for part in body.content]
    )

    # Persist the user prompt BEFORE the API call so the conversation reflects
    # what was sent even if the model call later fails. The assistant message
    # follows from billing finalization inside process_chat_request.
    #
    # Concurrency (design §0.1): take the conversation row lock (SELECT … FOR
    # UPDATE) and derive the parent from the LOCKED active path — NOT from the
    # possibly-stale conv.active_leaf_id. Two concurrent sends on one
    # conversation would otherwise both read the same leaf and create TWO active
    # user leaves, splitting the active path so the next turn leaks both users'
    # turns into context + billing and breaks the §8 invariant (exactly one
    # active leaf). Under the lock we also advance active_leaf_id to this user
    # node so a concurrent send chains off it instead of forking a sibling leaf.
    # The lock is held only across this short INSERT + leaf repoint and released
    # at the commit below — never across the long LLM stream. STEP 7 then parents
    # the assistant off this user node (the latest active leaf) and re-advances
    # the leaf to the assistant.
    conv = await lock_conversation(db, conversation_id)
    active = await resolve_active_path(db, conversation_id)
    parent_id = active[-1].id if active else None
    saved_message = await save_user_message(
        db,
        conv.id,
        content_to_text(structured_content),
        parent_id=parent_id,
    )
    # Capture the id into a plain var BEFORE the commit below expires the ORM
    # object — STEP-7 wires the assistant off this exact user node (§0.1), and a
    # post-commit attribute access would trigger a sync lazy load (MissingGreenlet).
    saved_message_id = saved_message.id
    await repoint_active_leaf(db, conv, saved_message_id)

    # Persist any attached images alongside the message (gallery + context
    # re-injection). Fire-and-forget: a failure here must NEVER block the chat
    # response, so we log and continue with the core flow intact.
    try:
        await save_message_images(
            db,
            user_id=user.id,
            conversation_id=conv.id,
            message_id=saved_message.id,
            content=structured_content,
        )
    except Exception:  # noqa: BLE001 — persistence is best-effort
        logger.warning(
            "Failed to persist chat images for message %s", saved_message.id,
            exc_info=True,
        )

    # Persist any attached files alongside the message (Files view + context
    # re-injection). Same best-effort posture as images.
    try:
        await save_message_files(
            db,
            user_id=user.id,
            conversation_id=conv.id,
            message_id=saved_message.id,
            content=structured_content,
        )
    except Exception:  # noqa: BLE001 — persistence is best-effort
        logger.warning(
            "Failed to persist chat files for message %s", saved_message.id,
            exc_info=True,
        )

    await db.commit()

    user_id = user.id
    excluded_image_ids = frozenset(body.excluded_image_ids)
    excluded_file_ids = frozenset(body.excluded_file_ids)

    # Bound concurrent streams per worker. locked() is True only when no slot is
    # free; there is no await between the check and acquire(), so in a single-
    # threaded event loop this is atomic — acquire() cannot then block.
    if _CHAT_STREAM_SEMAPHORE.locked():
        raise HTTPException(
            status_code=503,
            detail="Слишком много активных чатов, попробуйте через несколько секунд",
        )
    await _CHAT_STREAM_SEMAPHORE.acquire()

    async def event_generator():
        try:
            async for sse_event in process_chat_request(
                user_id=user_id,
                conversation_id=conversation_id,
                message_content=structured_content,
                model_id=body.model,
                db=db,
                redis=redis,
                is_disconnected=request.is_disconnected,
                web_search_enabled=body.web_search_enabled,
                thinking_enabled=body.thinking_enabled,
                thinking_effort=body.thinking_effort,
                tools_enabled=body.tools_enabled,
                excluded_image_ids=excluded_image_ids,
                excluded_file_ids=excluded_file_ids,
                # §0.1: the assistant is parented off the user node just saved
                # above (the active leaf), so a concurrent edit/regenerate on the
                # same turn can't mis-derive its parent.
                assistant_parent_id=saved_message_id,
            ):
                yield _sse_line(sse_event.event, sse_event.data)
        except TrinityError as exc:
            # Pre-stream failures (InsufficientFundsError, ContextTooLongError,
            # RateLimitedError, ModelUnavailableError, …) surface as a single
            # error SSE event so callers see the same wire format whether the
            # failure happened before or during streaming (SPEC.md §7.3).
            # Flatten exc.details into the payload so clients can render
            # context-specific info (e.g. required/available for insufficient_funds).
            payload: dict = {"code": exc.code, "message": exc.message}
            if exc.details:
                payload.update(exc.details)
            yield _sse_line("error", payload)
        finally:
            _CHAT_STREAM_SEMAPHORE.release()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── Edit a user message (SSE) ─────────────────────────────────────────────


@router.post("/conversations/{conversation_id}/messages/{message_id}/edit")
async def edit_message(
    conversation_id: UUID,
    message_id: UUID,
    body: EditMessageRequest,
    request: Request,
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> StreamingResponse:
    """Edit an active user message (text only) and stream a fresh assistant reply.

    Branch-and-set-aside (design §5): the original user message and the active
    tail after it are deactivated (never deleted). A new user sibling is inserted
    (same parent_id, is_active=true, edited content); the original message's
    images/files are COPIED onto it; the active leaf is repointed to the new user
    node so the active history ENDS at it. process_chat_request then runs with
    append_user_turn=False (§0.3 — the edited turn is already in the active
    history, appending would duplicate it) and STEP-7 (_wire_assistant_branch,
    §0.1) parents the new assistant off the new user node + advances the leaf to
    it. NO second re-parent here. The edit itself bills nothing — only the one
    forced generation is charged.
    """
    conv = await _get_owned_conversation(db, conversation_id, user.id)

    # Target MUST be an active, non-deleted USER message in this conversation.
    target = (
        await db.execute(
            select(Message).where(
                Message.id == message_id,
                Message.conversation_id == conv.id,
                Message.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")
    if target.role != "user":
        raise HTTPException(
            status_code=400, detail="Редактировать можно только сообщение пользователя"
        )
    if not target.is_active:
        raise HTTPException(
            status_code=400, detail="Сообщение не на активной ветке"
        )

    # Capture the target id BEFORE any mutation/commit expires the ORM object —
    # we still need it for the attachment copy after deactivation.
    target_id = target.id
    target_parent_id = target.parent_id

    # Inherit the THINKING posture of the reply being replaced — the active
    # assistant child of this user turn — mirroring regenerate (which inherits
    # from target.reasoning_tokens). Read it as a plain bool BEFORE
    # deactivate_active_tail_from flips is_active, so editing a question in a
    # thinking chat doesn't silently downgrade the new answer to no-thinking.
    # Effort isn't stored per turn, so default to "medium" (same as regenerate).
    active_reply = (
        await db.execute(
            select(Message).where(
                Message.parent_id == target_id,
                Message.role == "assistant",
                Message.is_active.is_(True),
                Message.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    inherited_thinking = (
        active_reply is not None
        and active_reply.reasoning_tokens is not None
        and active_reply.reasoning_tokens > 0
    )
    # Inherit the WEB-SEARCH posture of that same active reply, mirroring
    # regenerate (which recovers it from the target's usage_log via
    # _was_web_search). Capture the request_id as a plain value BEFORE any
    # commit/expire — a post-commit attribute read would trigger a sync lazy
    # load (MissingGreenlet). Without this the edit defaults web_search to False,
    # silently dropping search on the regenerated answer in a web-search chat.
    active_reply_request_id = (
        active_reply.request_id if active_reply is not None else None
    )
    inherited_web_search = await _was_web_search(db, active_reply_request_id)

    # tr-1hm: the live composer toggles win over the inherited posture, so editing
    # a question in a tool/thinking/web chat keeps that mode; None ⇒ inherit (tools
    # default off — prior tool use isn't recoverable from the persisted reply).
    effective_thinking = (
        body.thinking_enabled if body.thinking_enabled is not None else inherited_thinking
    )
    effective_web_search = (
        body.web_search_enabled
        if body.web_search_enabled is not None
        else inherited_web_search
    )
    effective_effort = body.thinking_effort or "medium"
    effective_tools = body.tools_enabled if body.tools_enabled is not None else False
    # tr-77u: the live model picker wins over the conversation default, mirroring
    # send/regenerate. None ⇒ inherit conv.model_id. The override is also persisted
    # to the conversation below so the edited reply is priced/recorded against the
    # model the user selected in the picker, not the stale model from the prior send.
    effective_model = body.model or conv.model_id

    # Serialize the branch mutation against concurrent sends/edits/regenerates on
    # this conversation (design §8 concurrency). Deactivate the target + the
    # active tail after it, insert the edited user sibling, copy its attachments,
    # and repoint the active leaf to the new node — all under the row lock, which
    # is released at the commit below (never held across the stream). After this,
    # resolve_active_path ends at the new user sibling, so STEP-7 (§0.1) parents
    # the assistant off it.
    locked = await lock_conversation(db, conv.id)
    # The answer on screen before this edit. If the edited reply fails before any
    # content, restore_orphaned_answer can't help (the new user sibling has no
    # answer child of its own) — so we pass this leaf to revert to the prior
    # answered turn instead of stranding the edited question with no answer (tr-yqmh).
    prior_active_leaf_id = locked.active_leaf_id
    # Persist the model override for subsequent reads (parity with send/regenerate).
    if effective_model != conv.model_id:
        conv.model_id = effective_model
        conv.updated_at = datetime.now(timezone.utc)
    # Remember this edit's posture so reopening the chat restores it (tr-hsg.12).
    _persist_last_settings(
        conv,
        thinking_enabled=effective_thinking,
        thinking_effort=effective_effort,
        tools_enabled=effective_tools,
        web_search_enabled=effective_web_search,
    )
    await deactivate_active_tail_from(db, conv.id, target)

    new_user = await save_user_message(
        db, conv.id, body.content, parent_id=target_parent_id
    )
    new_user_id = new_user.id

    # Carry the original message's attachments onto the new node (design §2):
    # rows are copied, not shared. Best-effort per the image/file persistence
    # posture — a copy failure must not abort the edit.
    try:
        await copy_images(db, target_id, new_user_id)
        await copy_files(db, target_id, new_user_id)
    except Exception:  # noqa: BLE001 — attachment copy is best-effort
        logger.warning(
            "Failed to copy attachments from %s to %s", target_id, new_user_id,
            exc_info=True,
        )

    # The active path now ends at the new user sibling; repoint the leaf there so
    # a concurrent reader sees a consistent leaf until STEP-7 advances it to the
    # new assistant.
    await repoint_active_leaf(db, locked, new_user_id)
    await db.commit()

    user_id = user.id
    conv_id = conv.id
    model_id = effective_model
    edited_content = body.content

    if _CHAT_STREAM_SEMAPHORE.locked():
        raise HTTPException(
            status_code=503,
            detail="Слишком много активных чатов, попробуйте через несколько секунд",
        )
    await _CHAT_STREAM_SEMAPHORE.acquire()

    async def event_generator():
        try:
            # §0.3: no new user turn — the active history already ends at the
            # edited user sibling, so append_user_turn=False skips BOTH the
            # context append and the estimation append. message_content carries
            # the edited text only for the prompt-length metric. The context read
            # (build_history_with_images, is_active-filtered in Task 3) sees the
            # NEW sibling and NOT the deactivated original. STEP-7 (§0.1) wires
            # the assistant's parent_id + the active leaf — NO second re-parent.
            async for sse_event in process_chat_request(
                user_id=user_id,
                conversation_id=conv_id,
                message_content=edited_content,
                model_id=model_id,
                db=db,
                redis=redis,
                is_disconnected=request.is_disconnected,
                append_user_turn=False,
                # Live composer toggles override the replaced reply's posture
                # (tr-1hm); they fall back to the inherited posture / off when the
                # client omits them (parity with regenerate).
                web_search_enabled=effective_web_search,
                thinking_enabled=effective_thinking,
                thinking_effort=effective_effort,
                tools_enabled=effective_tools,
                # §0.1: parent the new assistant off the freshly inserted user
                # sibling (the edited turn), not "the last active node".
                assistant_parent_id=new_user_id,
                # tr-yqmh: on failure before content the new user sibling has no
                # answer to restore, so revert to the answer shown before the edit.
                restore_leaf_on_failure_id=prior_active_leaf_id,
            ):
                yield _sse_line(sse_event.event, sse_event.data)
        except TrinityError as exc:
            payload: dict = {"code": exc.code, "message": exc.message}
            if exc.details:
                payload.update(exc.details)
            yield _sse_line("error", payload)
        finally:
            _CHAT_STREAM_SEMAPHORE.release()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── Regenerate an assistant message (SSE) ─────────────────────────────────


async def _get_active_assistant_message(
    db: AsyncSession, conversation_id: UUID, message_id: UUID
) -> Message:
    """Fetch an active, non-deleted assistant message in this conversation, else 404."""
    msg = (
        await db.execute(
            select(Message).where(
                Message.id == message_id,
                Message.conversation_id == conversation_id,
                Message.role == "assistant",
                Message.is_active.is_(True),
                Message.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")
    return msg


async def _was_web_search(db: AsyncSession, request_id: UUID | None) -> bool:
    """Recover whether the original generation used web-search, from its usage_log.

    Defaults to False when there's no request_id (e.g. a partial node without one)
    or no matching usage_log — a safe, cheaper default for the inherited regen.
    """
    if request_id is None:
        return False
    from app.models.usage_log import UsageLog

    flag = (
        await db.execute(
            select(UsageLog.web_search_enabled).where(
                UsageLog.request_id == request_id
            )
        )
    ).scalar_one_or_none()
    return bool(flag)


@router.post("/conversations/{conversation_id}/messages/{message_id}/regenerate")
async def regenerate_message(
    conversation_id: UUID,
    message_id: UUID,
    body: RegenerateRequest,
    request: Request,
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> StreamingResponse:
    """Regenerate an assistant message as a NEW sibling version, streamed via SSE.

    Deactivates the target assistant message + everything after it on the active
    path, then runs an INDEPENDENT process_chat_request (fresh request_id ⇒ fresh
    reservation + debit + usage_log). The new assistant node becomes a sibling of
    the target (parent_id = target.parent_id) and the conversation's active leaf —
    wired by the shared STEP-7 rule (_wire_assistant_branch, §0.1), NOT re-parented
    here. Optional model / thinking / web-search overrides apply to this version
    only; omitted fields inherit the conversation model and the target's posture.
    """
    conv = await _get_owned_conversation(db, conversation_id, user.id)
    target = await _get_active_assistant_message(db, conv.id, message_id)
    # Capture the target's parent (the user turn it answered) into a plain var
    # BEFORE any mutation/commit expires the ORM object — STEP-7 parents the new
    # assistant off it, making it a sibling/version of the target (§0.1).
    target_parent_id = target.parent_id
    # The exact version on screen — restored if the regenerate fails before
    # producing content, so a non-newest version isn't silently swapped (tr-hvr1).
    target_id = target.id

    # Resolve effective generation knobs: an override wins, else inherit. Model
    # falls back to the conversation default; thinking inherits the target's own
    # posture (it carried reasoning tokens); web-search is recoverable from the
    # target's usage_log.
    effective_model = body.model or conv.model_id
    web_search_enabled = (
        body.web_search_enabled
        if body.web_search_enabled is not None
        else await _was_web_search(db, target.request_id)
    )
    thinking_enabled = (
        body.thinking_enabled
        if body.thinking_enabled is not None
        else (target.reasoning_tokens is not None and target.reasoning_tokens > 0)
    )
    thinking_effort = body.thinking_effort or "medium"
    # tr-1hm: honor the live «Инструменты» toggle so a regenerated tool-answer
    # actually runs the agentic loop. None ⇒ off (we can't recover prior tool use
    # from the persisted reply, and a default-on would risk surprise spend).
    tools_enabled = body.tools_enabled if body.tools_enabled is not None else False

    # Persist the model override for subsequent reads (parity with send_message).
    if effective_model != conv.model_id:
        conv.model_id = effective_model
        conv.updated_at = datetime.now(timezone.utc)

    # Remember this regeneration's posture so reopening restores it (tr-hsg.12).
    _persist_last_settings(
        conv,
        thinking_enabled=thinking_enabled,
        thinking_effort=thinking_effort,
        tools_enabled=tools_enabled,
        web_search_enabled=web_search_enabled,
    )

    # Deactivate the target + the active tail after it BEFORE estimation/streaming,
    # so the is_active-filtered history read inside process_chat_request excludes
    # the stale tail and the replaced sibling. After this, resolve_active_path ends
    # at the parent user message the target answered — STEP-7 will then parent the
    # new assistant off it (= a sibling/version of the target). Lock the
    # conversation row so two concurrent regenerates can't both flip is_active.
    # The lock is released at the commit below — never held across the stream.
    locked = await lock_conversation(db, conv.id)
    await deactivate_active_tail_from(db, conv.id, target)
    # The active path now ends at the parent user message; repoint the leaf there
    # so a concurrent reader sees a consistent leaf until STEP-7 advances it to the
    # new assistant.
    active = await resolve_active_path(db, conv.id)
    await repoint_active_leaf(
        db, locked, active[-1].id if active else None
    )
    # The prompt being re-answered (the parent user message), for prompt-length
    # logging only — process_chat_request does NOT append it to context/estimation
    # when append_user_turn=False (§0.3); the active history already ends at it.
    regen_prompt = active[-1].content if active else ""
    await db.commit()

    user_id = user.id
    conv_id = conv.id

    if _CHAT_STREAM_SEMAPHORE.locked():
        raise HTTPException(
            status_code=503,
            detail="Слишком много активных чатов, попробуйте через несколько секунд",
        )
    await _CHAT_STREAM_SEMAPHORE.acquire()

    async def event_generator():
        try:
            async for sse_event in process_chat_request(
                user_id=user_id,
                conversation_id=conv_id,
                # §0.3: no new user turn. The active history already ends at the
                # parent user message to re-answer, so we pass append_user_turn=
                # False — process_chat_request skips BOTH the context append and
                # the estimation append. message_content is used only for the
                # prompt-length metric, so we pass the original prompt text.
                message_content=regen_prompt,
                model_id=effective_model,
                db=db,
                redis=redis,
                is_disconnected=request.is_disconnected,
                web_search_enabled=web_search_enabled,
                thinking_enabled=thinking_enabled,
                thinking_effort=thinking_effort,
                tools_enabled=tools_enabled,
                append_user_turn=False,
                # §0.1: parent the regenerated assistant off the SAME user turn
                # the target answered, so it becomes a sibling/version of the
                # target (not "the last active node", which races with edits).
                assistant_parent_id=target_parent_id,
                restore_leaf_on_failure_id=target_id,
            ):
                # §0.1: the new assistant node's parent_id + active leaf are wired
                # by STEP-7 (_wire_assistant_branch). NO second re-parent here.
                yield _sse_line(sse_event.event, sse_event.data)
        except TrinityError as exc:
            payload: dict = {"code": exc.code, "message": exc.message}
            if exc.details:
                payload.update(exc.details)
            yield _sse_line("error", payload)
        finally:
            _CHAT_STREAM_SEMAPHORE.release()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── Switch active version (free, non-streaming) ───────────────────────────


@router.put(
    "/conversations/{conversation_id}/active-path",
    response_model=list[MessageResponse],
    dependencies=[_conversation_bp],
)
async def switch_active_path(
    conversation_id: UUID,
    body: ActivePathSwitchRequest,
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> list[MessageResponse]:
    """Switch the rendered branch to the version rooted at body.node_id.

    FREE by construction: no model call, no reservation, no transaction, no
    usage_log — only is_active and active_leaf_id are rewritten (BILLING_LOGIC.md:
    switching never touches the balance). Under SELECT … FOR UPDATE on the
    conversation row we walk DOWN from the chosen node to its most-recent leaf,
    then `set_active_leaf_to` rewrites the whole active path to the single
    coherent root→leaf chain through it. The lock is held across the whole short
    read-modify-write, serializing it against concurrent
    sends/edits/regenerates/switches so exactly one active leaf survives (§8).
    """
    conv = await _get_owned_conversation(db, conversation_id, user.id)
    conv = await lock_conversation(db, conv.id)

    # The target must be a real, non-deleted node of THIS conversation.
    target = (
        await db.execute(
            select(Message).where(
                Message.id == body.node_id,
                Message.conversation_id == conv.id,
                Message.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="Версия не найдена")

    # 1) Walk DOWN from the chosen node to its leaf. Among a node's children we
    #    follow the most-recent one (created_at DESC, id DESC) so re-rendering a
    #    past turn restores its latest continuation deterministically. We only
    #    resolve the leaf id here — set_active_leaf_to does all the is_active
    #    rewriting, so no per-node mutation in the walk.
    leaf_id = target.id
    node_id = target.id
    while True:
        child = (
            await db.execute(
                select(Message)
                .where(
                    Message.parent_id == node_id,
                    Message.deleted_at.is_(None),
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if child is None:
            break
        node_id = child.id
        leaf_id = child.id

    # 2) Rewrite the active path to the single coherent chain ending at the leaf.
    #    This deactivates EVERY other active node (including a different branch
    #    left active by an earlier edit that set the target's ancestors aside) and
    #    re-activates the chosen node's full ancestor chain — so the switch can
    #    never leave two active leaves, regardless of the prior state. It also sets
    #    active_leaf_id, so no separate repoint_active_leaf call is needed.
    await set_active_leaf_to(db, conv, leaf_id)
    conv.updated_at = datetime.now(timezone.utc)
    await db.commit()

    # 4) Re-derive and return the new active path (root → leaf, active branch).
    #    version_info is computed over the WHOLE conversation (same as GET) so each
    #    returned node carries its real version_index/version_count — without it the
    #    nodes default to (1, 1) and the frontend collapses the < n/m > switcher
    #    after a switch, stranding the user on the chosen version (no way back).
    version_info = await derive_version_info(db, conv.id)
    path = await resolve_active_path(db, conv.id)
    return [_to_message_response(m, version_info) for m in path]
