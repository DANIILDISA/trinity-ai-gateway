import hashlib
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.constants import micro_to_api_string
from app.core.security import blacklist_access_token, decode_access_token
from app.dependencies import get_db, get_redis, require_auth
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.notification import Notification
from app.models.refresh_token import RefreshToken
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.user import (
    AnalyticsResponse,
    DeleteAccountRequest,
    ModelUsageBreakdown,
    NotificationItem,
    NotificationListResponse,
    SessionItem,
    SessionListResponse,
    TransactionHistoryResponse,
    TransactionItem,
    UpdateProfileRequest,
    UserProfile,
)
from app.services.auth_service import schedule_account_deletion
from redis.asyncio import Redis

router = APIRouter()


def _to_profile(user: User) -> UserProfile:
    return UserProfile(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        full_name=user.full_name,
        role=user.role,
        status=user.status,
        balance=micro_to_api_string(user.balance_micro_rub),
        reserved=micro_to_api_string(user.reserved_micro_rub),
        available=micro_to_api_string(
            max(user.balance_micro_rub - user.reserved_micro_rub, 0)
        ),
        has_password=user.password_hash is not None,
        scheduled_delete_at=user.scheduled_delete_at,
    )


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@router.get("/me", response_model=UserProfile)
async def get_me(user: User = Depends(require_auth)) -> UserProfile:
    return _to_profile(user)


@router.patch("/me", response_model=UserProfile)
async def update_me(
    body: UpdateProfileRequest,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> UserProfile:
    # PATCH semantics: only touch fields the client sent. Empty string clears
    # full_name (so the user can wipe it from the UI); display_name must stay
    # non-empty so we ignore "" there.
    if body.display_name is not None and body.display_name.strip():
        user.display_name = body.display_name.strip()
    if body.full_name is not None:
        trimmed = body.full_name.strip()
        user.full_name = trimmed if trimmed else None
    await db.commit()
    return _to_profile(user)


@router.get("/me/transactions", response_model=TransactionHistoryResponse)
async def get_transactions(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> TransactionHistoryResponse:
    total = (
        await db.execute(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.user_id == user.id)
        )
    ).scalar() or 0

    pages = (total + per_page - 1) // per_page if total > 0 else 0

    rows = (
        await db.execute(
            select(Transaction)
            .where(Transaction.user_id == user.id)
            .order_by(Transaction.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).scalars().all()

    items = [
        TransactionItem(
            id=t.id,
            type=t.type,
            amount=micro_to_api_string(t.amount_micro_rub),
            description=t.description,
            balance_after=micro_to_api_string(t.balance_after_micro_rub),
            created_at=t.created_at,
        )
        for t in rows
    ]

    return TransactionHistoryResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )


@router.get("/me/notifications", response_model=NotificationListResponse)
async def list_notifications(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> NotificationListResponse:
    total = (
        await db.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user.id)
        )
    ).scalar() or 0
    pages = (total + per_page - 1) // per_page if total > 0 else 0

    rows = (
        await db.execute(
            select(Notification)
            .where(Notification.user_id == user.id)
            .order_by(Notification.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).scalars().all()

    items = [
        NotificationItem(
            id=n.id,
            type=n.type,
            title=n.title,
            message=n.message,
            channel=n.channel,
            is_read=n.is_read,
            created_at=n.created_at,
        )
        for n in rows
    ]

    return NotificationListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )


@router.post("/me/notifications/{notification_id}/read", status_code=204)
async def mark_notification_read(
    notification_id: UUID,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> None:
    note = (
        await db.execute(
            select(Notification).where(
                Notification.id == notification_id,
                Notification.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if note is None:
        raise HTTPException(status_code=404, detail="Уведомление не найдено")
    if not note.is_read:
        note.is_read = True
        await db.commit()


# ─── Active sessions ────────────────────────────────────────────────────────

@router.get("/me/sessions", response_model=SessionListResponse)
async def list_sessions(
    request: Request,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> SessionListResponse:
    """Refresh tokens map 1:1 to active device sessions. We surface them in
    settings so the user can see and revoke individual devices."""
    rows = (
        await db.execute(
            select(RefreshToken)
            .where(RefreshToken.user_id == user.id)
            .order_by(RefreshToken.created_at.desc())
        )
    ).scalars().all()

    current_cookie = request.cookies.get("refresh_token")
    current_hash = _token_hash(current_cookie) if current_cookie else None

    items = [
        SessionItem(
            id=t.id,
            ip_address=str(t.ip_address) if t.ip_address is not None else None,
            user_agent=t.user_agent,
            created_at=t.created_at,
            expires_at=t.expires_at,
            is_current=(current_hash is not None and t.token_hash == current_hash),
        )
        for t in rows
    ]
    return SessionListResponse(items=items)


@router.delete("/me/sessions/{session_id}", status_code=204)
async def terminate_session(
    session_id: UUID,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> None:
    token_row = (
        await db.execute(
            select(RefreshToken).where(
                RefreshToken.id == session_id,
                RefreshToken.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if token_row is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    await db.delete(token_row)
    await db.commit()


@router.post("/me/sessions/terminate-all")
async def terminate_all_sessions(
    request: Request,
    response: Response,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, str]:
    """Revoke every refresh token and blacklist the current access token so
    *all* devices — including this one — are signed out."""
    await db.execute(sa_delete(RefreshToken).where(RefreshToken.user_id == user.id))
    await db.commit()

    auth_header = request.headers.get("Authorization", "")
    _, _, token = auth_header.partition(" ")
    if token:
        # Decode is outside the blocklist-write guard: an invalid/expired token here just
        # means no jti to blacklist (require_auth already validated the caller). tr-34t3 FU-2.
        try:
            payload = await decode_access_token(token)
            jti, exp = payload.get("jti"), payload.get("exp")
        except Exception:
            jti = exp = None
        if jti and exp:
            await blacklist_access_token(redis, jti, int(exp))

    response.delete_cookie("refresh_token")
    return {"message": "Все сессии завершены"}


# ─── Soft-delete account ────────────────────────────────────────────────────

@router.post("/me/delete")
async def delete_account(
    body: DeleteAccountRequest,
    request: Request,
    response: Response,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, str]:
    """Mark the account as pending deletion. Requires the user to retype
    their email exactly so a stray click can't trigger this. Recovery is via
    POST /auth/recover with the same email + password before the (future)
    hard-delete sweep runs."""
    if body.email_confirmation.strip().lower() != user.email.lower():
        raise HTTPException(status_code=400, detail="Email не совпадает")

    await schedule_account_deletion(db, user, ip_address=_client_ip(request))

    # Blacklist the current access token so the session ends immediately.
    auth_header = request.headers.get("Authorization", "")
    _, _, token = auth_header.partition(" ")
    if token:
        # Decode outside the blocklist-write guard (see terminate_all_sessions). tr-34t3 FU-2.
        try:
            payload = await decode_access_token(token)
            jti, exp = payload.get("jti"), payload.get("exp")
        except Exception:
            jti = exp = None
        if jti and exp:
            await blacklist_access_token(redis, jti, int(exp))

    response.delete_cookie("refresh_token")
    return {"message": "Аккаунт помечен на удаление"}


# ─── Analytics ─────────────────────────────────────────────────────────────

@router.get("/me/analytics", response_model=AnalyticsResponse)
async def get_analytics(
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> AnalyticsResponse:
    """Aggregate the user's lifetime usage. Messages and per-model token
    counts come from the messages table; total_spent comes from debit
    transactions so it reflects what was actually billed (including
    overdraft absorption and refunds)."""
    msg_query = (
        select(func.count(Message.id))
        .select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.user_id == user.id, Message.role == "user")
    )
    total_messages = (await db.execute(msg_query)).scalar() or 0

    # Token totals: assistant messages carry the input/output token counts
    # billed by OpenRouter, so aggregate from there for accuracy.
    tok_query = (
        select(
            func.coalesce(func.sum(Message.input_tokens), 0),
            func.coalesce(func.sum(Message.output_tokens), 0),
        )
        .select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.user_id == user.id, Message.role == "assistant")
    )
    total_input_tokens, total_output_tokens = (await db.execute(tok_query)).one()

    spent_query = select(func.coalesce(func.sum(-Transaction.amount_micro_rub), 0)).where(
        Transaction.user_id == user.id, Transaction.type == "debit"
    )
    total_spent_micro = (await db.execute(spent_query)).scalar() or 0

    by_model_query = (
        select(
            Message.model_id,
            func.count(Message.id).label("msgs"),
            func.coalesce(func.sum(Message.input_tokens), 0).label("ins"),
            func.coalesce(func.sum(Message.output_tokens), 0).label("outs"),
            func.coalesce(func.sum(Message.cost_micro_rub), 0).label("spent"),
        )
        .select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.user_id == user.id,
            Message.role == "assistant",
            Message.model_id.isnot(None),
        )
        .group_by(Message.model_id)
        .order_by(func.coalesce(func.sum(Message.cost_micro_rub), 0).desc())
    )
    by_model_rows = (await db.execute(by_model_query)).all()
    by_model = [
        ModelUsageBreakdown(
            model_id=row.model_id,
            messages=int(row.msgs),
            input_tokens=int(row.ins),
            output_tokens=int(row.outs),
            spent=micro_to_api_string(int(row.spent or 0)),
        )
        for row in by_model_rows
    ]

    return AnalyticsResponse(
        total_messages=int(total_messages or 0),
        total_input_tokens=int(total_input_tokens or 0),
        total_output_tokens=int(total_output_tokens or 0),
        total_spent=micro_to_api_string(int(total_spent_micro)),
        by_model=by_model,
    )
