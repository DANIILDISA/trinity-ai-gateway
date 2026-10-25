"""User-facing image endpoints: gallery listing + per-conversation context.

The gallery returns thumbnails only (never the full payload) so paging stays
cheap; the conversation endpoint returns full base64 so the frontend can
re-inject images into multimodal context.
"""
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_active
from app.models.chat_image import ChatImage
from app.models.conversation import Conversation
from app.models.user import User

router = APIRouter()


class GalleryImage(BaseModel):
    id: UUID
    thumbnail_base64: str
    media_type: str
    created_at: datetime
    conversation_id: UUID | None
    file_size_bytes: int
    # True when the owning conversation is archived (soft-deleted). The image
    # stays in the gallery but the frontend renders it dimmed. Orphan images
    # (conversation_id IS NULL) are never archived.
    archived: bool


class GalleryResponse(BaseModel):
    items: list[GalleryImage]
    total: int
    page: int
    has_more: bool


class ContextImage(BaseModel):
    id: UUID
    message_id: UUID | None
    full_base64: str
    media_type: str
    created_at: datetime
    # The persisted "в контексте" toggle — false means the user excluded this
    # image from the model context (tr-kx4f).
    included_in_context: bool


class ImageContextUpdate(BaseModel):
    included_in_context: bool


@router.get("/gallery", response_model=GalleryResponse)
async def get_gallery(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> GalleryResponse:
    total = (
        await db.execute(
            select(func.count())
            .select_from(ChatImage)
            .where(ChatImage.user_id == user.id)
        )
    ).scalar() or 0

    rows = (
        await db.execute(
            select(
                ChatImage.id,
                ChatImage.thumbnail_base64,
                ChatImage.media_type,
                ChatImage.created_at,
                ChatImage.conversation_id,
                ChatImage.file_size_bytes,
                Conversation.deleted_at,
            )
            # LEFT JOIN so orphan images (conversation_id IS NULL) still appear.
            .outerjoin(Conversation, ChatImage.conversation_id == Conversation.id)
            .where(ChatImage.user_id == user.id)
            .order_by(ChatImage.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).all()

    items = [
        GalleryImage(
            id=r.id,
            thumbnail_base64=r.thumbnail_base64,
            media_type=r.media_type,
            created_at=r.created_at,
            conversation_id=r.conversation_id,
            file_size_bytes=r.file_size_bytes,
            archived=r.deleted_at is not None,
        )
        for r in rows
    ]
    return GalleryResponse(
        items=items,
        total=total,
        page=page,
        has_more=(page * per_page) < total,
    )


@router.get("/conversation/{conversation_id}", response_model=list[ContextImage])
async def get_conversation_images(
    conversation_id: UUID,
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> list[ContextImage]:
    # Verify ownership before exposing the full payloads. 404 (not 403) on a
    # foreign conversation mirrors the chat endpoints' non-leaking posture.
    owns = (
        await db.execute(
            select(Conversation.id).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if owns is None:
        raise HTTPException(status_code=404, detail="Диалог не найден")

    rows = (
        await db.execute(
            select(
                ChatImage.id,
                ChatImage.message_id,
                ChatImage.full_base64,
                ChatImage.media_type,
                ChatImage.created_at,
                ChatImage.included_in_context,
            )
            .where(ChatImage.conversation_id == conversation_id)
            .order_by(ChatImage.created_at.asc())
        )
    ).all()

    return [
        ContextImage(
            id=r.id,
            message_id=r.message_id,
            full_base64=r.full_base64,
            media_type=r.media_type,
            created_at=r.created_at,
            included_in_context=r.included_in_context,
        )
        for r in rows
    ]


@router.patch("/{image_id}", response_model=ContextImage)
async def update_image_context(
    image_id: UUID,
    body: ImageContextUpdate,
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> ContextImage:
    """Persist the per-image "в контексте" toggle.

    Ownership is enforced on the image's own user_id (404 on a foreign image,
    mirroring the non-leaking posture elsewhere). Persisting the flag lets the
    choice survive reload AND lets the context builder skip excluded images on
    later turns instead of re-billing them for vision (tr-kx4f).
    """
    img = (
        await db.execute(
            select(ChatImage).where(
                ChatImage.id == image_id,
                ChatImage.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if img is None:
        raise HTTPException(status_code=404, detail="Изображение не найдено")

    img.included_in_context = body.included_in_context
    await db.commit()
    await db.refresh(img)

    return ContextImage(
        id=img.id,
        message_id=img.message_id,
        full_base64=img.full_base64,
        media_type=img.media_type,
        created_at=img.created_at,
        included_in_context=img.included_in_context,
    )
