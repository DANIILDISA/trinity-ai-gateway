"""User-facing file endpoints: Files-view listing + per-conversation context.

Mirrors images.py. The gallery/listing and the conversation endpoint return
metadata only (never the base64 payload) so paging and history loads stay cheap;
a single-file endpoint returns the payload on demand for viewing/downloading.
"""
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_active
from app.models.artifact import Artifact
from app.models.chat_file import ChatFile
from app.models.conversation import Conversation
from app.models.user import User

router = APIRouter()


class GalleryArtifact(BaseModel):
    """One AI-authored file in the gallery's "Созданные ИИ" mode: the LATEST
    version of a logical file (distinct group_id). `version_count` is how many
    versions exist for that group. Downloaded via the existing per-conversation
    artifact endpoint, keyed by (conversation_id, filename)."""

    group_id: UUID
    filename: str
    media_type: str
    size_bytes: int
    version: int
    version_count: int
    conversation_id: UUID | None
    created_at: datetime


class GalleryArtifactsResponse(BaseModel):
    items: list[GalleryArtifact]
    total: int
    page: int
    has_more: bool


class GalleryFile(BaseModel):
    id: UUID
    filename: str
    media_type: str
    kind: str
    file_size_bytes: int
    token_estimate: int
    created_at: datetime
    conversation_id: UUID | None
    # True when the owning conversation is archived (soft-deleted) — rendered
    # dimmed in the Files view, same as gallery images.
    archived: bool


class GalleryFilesResponse(BaseModel):
    items: list[GalleryFile]
    total: int
    page: int
    has_more: bool


class ContextFile(BaseModel):
    """Per-conversation file metadata for analytics + the context checkbox.

    No payload here — the chip fetches it lazily from GET /files/{id} when the
    user opens or downloads the file.
    """

    id: UUID
    message_id: UUID | None
    filename: str
    media_type: str
    kind: str
    file_size_bytes: int
    token_estimate: int
    created_at: datetime
    # The persisted "в контексте" toggle — false means the user excluded this
    # file from the model context (tr-kx4f).
    included_in_context: bool


class FileContextUpdate(BaseModel):
    included_in_context: bool


class FileDetail(BaseModel):
    id: UUID
    filename: str
    media_type: str
    kind: str
    base64_data: str


@router.get("/gallery", response_model=GalleryFilesResponse)
async def get_files_gallery(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> GalleryFilesResponse:
    total = (
        await db.execute(
            select(func.count())
            .select_from(ChatFile)
            .where(ChatFile.user_id == user.id)
        )
    ).scalar() or 0

    rows = (
        await db.execute(
            select(
                ChatFile.id,
                ChatFile.filename,
                ChatFile.media_type,
                ChatFile.kind,
                ChatFile.file_size_bytes,
                ChatFile.token_estimate,
                ChatFile.created_at,
                ChatFile.conversation_id,
                Conversation.deleted_at,
            )
            # LEFT JOIN so orphan files (conversation_id IS NULL) still appear.
            .outerjoin(Conversation, ChatFile.conversation_id == Conversation.id)
            .where(ChatFile.user_id == user.id)
            .order_by(ChatFile.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).all()

    items = [
        GalleryFile(
            id=r.id,
            filename=r.filename,
            media_type=r.media_type,
            kind=r.kind,
            file_size_bytes=r.file_size_bytes,
            token_estimate=r.token_estimate,
            created_at=r.created_at,
            conversation_id=r.conversation_id,
            archived=r.deleted_at is not None,
        )
        for r in rows
    ]
    return GalleryFilesResponse(
        items=items,
        total=total,
        page=page,
        has_more=(page * per_page) < total,
    )


@router.get("/artifacts", response_model=GalleryArtifactsResponse)
async def get_artifacts_gallery(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> GalleryArtifactsResponse:
    """List the user's AI-authored artifacts for the gallery's opt-in "Созданные
    ИИ" mode: the LATEST version per logical file (distinct group_id), newest
    first. Each row carries `version_count` (versions in the group) so the card
    can show <n versions>. Ownership-scoped on the artifact's own user_id (so a
    deleted conversation still keeps the owner's files). Excludes uploads
    (chat_files) entirely. Download reuses GET /chat/conversations/{id}/artifact."""
    # One logical file == one group_id. There are far fewer groups than versions,
    # so count distinct groups for the page total.
    total = (
        await db.execute(
            select(func.count(func.distinct(Artifact.group_id)))
            .where(Artifact.user_id == user.id)
        )
    ).scalar() or 0

    # Latest-per-group in one round-trip: DISTINCT ON (group_id) keeps the first
    # row per group under the (group_id, version DESC) ordering — i.e. the newest
    # version — while a window count over the group yields version_count without a
    # second query. The outer query then re-sorts those latest rows newest-first.
    version_count = func.count().over(partition_by=Artifact.group_id)
    latest = (
        select(
            Artifact.group_id,
            Artifact.filename,
            Artifact.media_type,
            Artifact.size_bytes,
            Artifact.version,
            Artifact.conversation_id,
            Artifact.created_at,
            version_count.label("version_count"),
        )
        .where(Artifact.user_id == user.id)
        .distinct(Artifact.group_id)
        .order_by(Artifact.group_id, Artifact.version.desc())
        .subquery()
    )

    rows = (
        await db.execute(
            select(latest)
            .order_by(latest.c.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).all()

    items = [
        GalleryArtifact(
            group_id=r.group_id,
            filename=r.filename,
            media_type=r.media_type,
            size_bytes=r.size_bytes,
            version=r.version,
            version_count=r.version_count,
            conversation_id=r.conversation_id,
            created_at=r.created_at,
        )
        for r in rows
    ]
    return GalleryArtifactsResponse(
        items=items,
        total=total,
        page=page,
        has_more=(page * per_page) < total,
    )


@router.get("/conversation/{conversation_id}", response_model=list[ContextFile])
async def get_conversation_files(
    conversation_id: UUID,
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> list[ContextFile]:
    # Verify ownership; 404 (not 403) on a foreign conversation mirrors the chat
    # endpoints' non-leaking posture.
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
                ChatFile.id,
                ChatFile.message_id,
                ChatFile.filename,
                ChatFile.media_type,
                ChatFile.kind,
                ChatFile.file_size_bytes,
                ChatFile.token_estimate,
                ChatFile.created_at,
                ChatFile.included_in_context,
            )
            .where(ChatFile.conversation_id == conversation_id)
            .order_by(ChatFile.created_at.asc())
        )
    ).all()

    return [
        ContextFile(
            id=r.id,
            message_id=r.message_id,
            filename=r.filename,
            media_type=r.media_type,
            kind=r.kind,
            file_size_bytes=r.file_size_bytes,
            token_estimate=r.token_estimate,
            created_at=r.created_at,
            included_in_context=r.included_in_context,
        )
        for r in rows
    ]


@router.get("/{file_id}", response_model=FileDetail)
async def get_file(
    file_id: UUID,
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> FileDetail:
    """Return a single file's payload (base64) for viewing / downloading.

    Ownership is enforced on the file's own user_id, so orphan files (their
    conversation was deleted) remain accessible to their owner.
    """
    row = (
        await db.execute(
            select(
                ChatFile.id,
                ChatFile.filename,
                ChatFile.media_type,
                ChatFile.kind,
                ChatFile.base64_data,
            ).where(
                ChatFile.id == file_id,
                ChatFile.user_id == user.id,
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileDetail(
        id=row.id,
        filename=row.filename,
        media_type=row.media_type,
        kind=row.kind,
        base64_data=row.base64_data,
    )


@router.patch("/{file_id}", response_model=ContextFile)
async def update_file_context(
    file_id: UUID,
    body: FileContextUpdate,
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> ContextFile:
    """Persist the per-file "в контексте" toggle.

    Ownership is enforced on the file's own user_id (404 on a foreign file).
    Persisting the flag lets the choice survive reload AND lets the context
    builder skip excluded files on later turns (tr-kx4f).
    """
    f = (
        await db.execute(
            select(ChatFile).where(
                ChatFile.id == file_id,
                ChatFile.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if f is None:
        raise HTTPException(status_code=404, detail="Файл не найден")

    f.included_in_context = body.included_in_context
    await db.commit()
    await db.refresh(f)

    return ContextFile(
        id=f.id,
        message_id=f.message_id,
        filename=f.filename,
        media_type=f.media_type,
        kind=f.kind,
        file_size_bytes=f.file_size_bytes,
        token_estimate=f.token_estimate,
        created_at=f.created_at,
        included_in_context=f.included_in_context,
    )
