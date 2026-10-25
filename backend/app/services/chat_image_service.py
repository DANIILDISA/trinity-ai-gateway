"""Persistence and retrieval for images attached to chat messages.

Images sent with a user message are stored verbatim (base64) so they can be
shown in the gallery and re-injected into multimodal context on later turns.
Persistence is fire-and-forget at the call site: a failure here must never
break the chat flow (the caller logs and continues).
"""
import base64
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_image import ChatImage
from app.models.message import Message

logger = logging.getLogger(__name__)


def _parse_data_uri(url: str) -> tuple[str, str]:
    """Split a `data:{media_type};base64,{payload}` URI into (media_type, payload).

    Raises ValueError if the URI is not a base64 data URI we can store.
    """
    if not url.startswith("data:"):
        raise ValueError("not a data URI")
    header, _, payload = url.partition(",")
    if not payload:
        raise ValueError("data URI has no payload")
    # header == "data:{media_type};base64"
    meta = header[len("data:"):]
    media_type, _, encoding = meta.partition(";")
    if encoding != "base64":
        raise ValueError("data URI is not base64-encoded")
    if not media_type:
        raise ValueError("data URI has no media type")
    return media_type, payload


async def save_message_images(
    db: AsyncSession,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    message_id: uuid.UUID,
    content: "str | list[dict]",
) -> int:
    """Persist image parts of a multimodal message. Returns the count stored.

    Adds rows to the session WITHOUT committing — the caller commits in the same
    unit of work as the user message. A plain string (text-only) is a no-op.
    Per spec, thumbnail_base64 == full_base64 (the frontend already sends a
    compact thumbnail-sized payload).
    """
    if not isinstance(content, list):
        return 0

    stored = 0
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "image_url":
            continue
        url = (part.get("image_url") or {}).get("url")
        if not isinstance(url, str):
            continue
        media_type, payload = _parse_data_uri(url)
        file_size_bytes = len(base64.b64decode(payload, validate=False))
        db.add(
            ChatImage(
                user_id=user_id,
                conversation_id=conversation_id,
                message_id=message_id,
                thumbnail_base64=payload,
                full_base64=payload,
                media_type=media_type,
                file_size_bytes=file_size_bytes,
            )
        )
        stored += 1
    return stored


async def copy_images(
    db: AsyncSession,
    src_message_id: uuid.UUID,
    dst_message_id: uuid.UUID,
) -> int:
    """Duplicate every chat_images row from src_message_id onto dst_message_id.

    Used by the edit-resend flow: the edited message is a NEW node (new id), so
    its attachments must be cloned (we copy rows, not share storage — design §10).
    Rows are added WITHOUT committing; the caller commits in the same unit of work
    as the new user node. Returns the count copied.
    """
    src_rows = (
        await db.execute(
            select(ChatImage).where(ChatImage.message_id == src_message_id)
        )
    ).scalars().all()
    for row in src_rows:
        db.add(
            ChatImage(
                user_id=row.user_id,
                conversation_id=row.conversation_id,
                message_id=dst_message_id,
                thumbnail_base64=row.thumbnail_base64,
                full_base64=row.full_base64,
                media_type=row.media_type,
                file_size_bytes=row.file_size_bytes,
                width=row.width,
                height=row.height,
                # Carry the toggle so excluding an image then editing the message
                # doesn't silently re-include the clone (tr-kx4f).
                included_in_context=row.included_in_context,
            )
        )
    return len(src_rows)


async def build_history_with_images(
    db: AsyncSession,
    conversation_id: uuid.UUID | None,
    excluded_image_ids: "frozenset[str] | None" = None,
    excluded_file_ids: "frozenset[str] | None" = None,
) -> list[dict]:
    """Conversation history as model-ready messages, with persisted images and
    files re-injected.

    Identical in ordering/filtering to message_service.get_conversation_messages
    (user/assistant only, created_at ASC then id ASC), so when a conversation has
    no stored attachments the output is byte-identical to the text-only history.
    User messages that carry non-excluded images/files become multimodal arrays
    (text first, then image_url parts, then file parts) so the model sees the
    attachments again. File parts stay raw here; the caller runs
    file_content.prepare_context_content to inline text files / flag the parser.
    """
    if conversation_id is None:
        return []

    excluded = excluded_image_ids or frozenset()

    rows = (
        await db.execute(
            select(Message.id, Message.role, Message.content)
            .where(
                Message.conversation_id == conversation_id,
                Message.role.in_(["user", "assistant"]),
                Message.is_active.is_(True),
                Message.deleted_at.is_(None),
            )
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    ).all()
    if not rows:
        return []

    images = (
        await db.execute(
            select(
                ChatImage.message_id,
                ChatImage.media_type,
                ChatImage.full_base64,
                ChatImage.id,
            )
            # included_in_context = false means the user excluded this image via the
            # "в контексте" toggle — skip it here so it is NOT re-injected (and not
            # re-billed for vision) on later turns (tr-kx4f).
            .where(
                ChatImage.conversation_id == conversation_id,
                ChatImage.included_in_context.is_(True),
            )
            .order_by(ChatImage.created_at.asc())
        )
    ).all()

    by_message: dict[uuid.UUID, list[str]] = {}
    for msg_id, media_type, full_base64, image_id in images:
        if msg_id is None or str(image_id) in excluded:
            continue
        by_message.setdefault(msg_id, []).append(
            f"data:{media_type};base64,{full_base64}"
        )

    # Persisted file parts re-injected the same way images are (parity with the
    # per-file context checkbox). Imported lazily to avoid a circular import.
    from app.services.chat_file_service import files_by_message

    files_by_msg = await files_by_message(db, conversation_id, excluded_file_ids)

    result: list[dict] = []
    for msg_id, role, content in rows:
        msg_images = by_message.get(msg_id) if role == "user" else None
        msg_files = files_by_msg.get(msg_id) if role == "user" else None
        if msg_images or msg_files:
            parts: list[dict] = []
            if content:
                parts.append({"type": "text", "text": content})
            parts.extend(
                {"type": "image_url", "image_url": {"url": uri}}
                for uri in (msg_images or [])
            )
            parts.extend(msg_files or [])
            result.append({"role": role, "content": parts})
        else:
            result.append({"role": role, "content": content})
    return result
