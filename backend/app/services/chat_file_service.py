"""Persistence and retrieval for non-image files attached to chat messages.

Mirrors chat_image_service: files sent with a user message are stored verbatim
(base64) so they can be shown in the Files view, opened/downloaded from history,
and re-injected into the model context on later turns. Persistence is
fire-and-forget at the call site — a failure here must never break the chat flow.
"""
import base64
import binascii
import hashlib
import logging
import os
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_file import ChatFile

logger = logging.getLogger(__name__)

# Mirrors the text MIME set in file_content.py / frontend lib/fileTypes.ts.
_TEXT_MIME_TYPES: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/html",
        "text/css",
        "application/json",
        "application/xml",
        "text/xml",
        "application/x-yaml",
        "text/yaml",
        "application/toml",
        "text/x-python",
        "application/javascript",
        "text/javascript",
        "application/typescript",
        "text/x-typescript",
        "text/x-go",
        "text/x-java",
        "text/x-rust",
        "application/x-sh",
        "application/sql",
    }
)
_PDF_MIME = "application/pdf"
_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

# Token-estimation constants — kept in lockstep with frontend lib/fileTypes.ts so
# the analytics prognosis (pending) and the persisted count agree.
_CHARS_PER_TOKEN = 4
_TOKENS_PER_PDF_PAGE = 500
_DOCX_BYTES_PER_TOKEN = 4
_PDF_PAGE_RE = re.compile(rb"/Type\s*/Page[^s]")


def _parse_data_uri(url: str) -> tuple[str, str]:
    """Split a `data:{media_type};base64,{payload}` URI into (media_type, payload)."""
    if not url.startswith("data:"):
        raise ValueError("not a data URI")
    header, _, payload = url.partition(",")
    if not payload:
        raise ValueError("data URI has no payload")
    meta = header[len("data:"):]
    media_type, _, encoding = meta.partition(";")
    if encoding != "base64":
        raise ValueError("data URI is not base64-encoded")
    if not media_type:
        raise ValueError("data URI has no media type")
    return media_type.strip().lower(), payload


def classify_kind(media_type: str) -> str:
    """Map a media type to the storage kind (text|pdf|docx). Defaults to text."""
    if media_type == _PDF_MIME:
        return "pdf"
    if media_type == _DOCX_MIME:
        return "docx"
    return "text"


def _estimate_tokens(kind: str, raw: bytes) -> int:
    """Per-kind token estimate mirroring frontend lib/fileTypes.ts."""
    if kind == "text":
        text = raw.decode("utf-8", errors="replace")
        return max(0, -(-len(text) // _CHARS_PER_TOKEN))  # ceil
    if kind == "pdf":
        pages = len(_PDF_PAGE_RE.findall(raw))
        if pages <= 0:
            pages = max(1, round(len(raw) / 3000))
        return pages * _TOKENS_PER_PDF_PAGE
    # docx / anything else: size-based proxy.
    return max(1, -(-len(raw) // _DOCX_BYTES_PER_TOKEN))


async def save_message_files(
    db: AsyncSession,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    message_id: uuid.UUID,
    content: "str | list[dict]",
) -> int:
    """Persist `file` parts of a multimodal message. Returns the count stored.

    Adds rows to the session WITHOUT committing — the caller commits in the same
    unit of work as the user message. A plain string (text-only) is a no-op.
    """
    if not isinstance(content, list):
        return 0

    stored = 0
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "file":
            continue
        file_obj = part.get("file") or {}
        file_data = file_obj.get("file_data")
        filename = (file_obj.get("filename") or "file")[:255]
        if not isinstance(file_data, str):
            continue
        media_type, payload = _parse_data_uri(file_data)
        try:
            raw = base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError):
            logger.warning("Skipping unparseable file %s", filename)
            continue
        kind = classify_kind(media_type)
        db.add(
            ChatFile(
                user_id=user_id,
                conversation_id=conversation_id,
                message_id=message_id,
                filename=filename,
                media_type=media_type,
                kind=kind,
                base64_data=payload,
                file_size_bytes=len(raw),
                token_estimate=_estimate_tokens(kind, raw),
            )
        )
        stored += 1
    return stored


async def _included_winning(
    db: AsyncSession, conversation_id: uuid.UUID, *, with_data: bool
) -> list[dict]:
    """The conversation's included chat_files that WIN the basename dedup (the LATEST upload
    of a given name by created_at), ordered. ``with_data=False`` skips the heavy ``base64_data``
    column (and ``media_type``, unused by the hash) so the B3 seed-hash path never reads file
    bytes. This is the single source of truth for which rows seed, so the hash and the full
    fetch can never drift on the winning set. chat_files rows are INSERT-ONLY — ``base64_data``
    is never updated — so a row ``id`` is a stable identity for its content (relied on by the
    seed-hash; see tr-l9ri for a defense-in-depth immutability trigger)."""
    cols = [ChatFile.filename, ChatFile.id]
    if with_data:
        cols += [ChatFile.media_type, ChatFile.base64_data]
    rows = (
        await db.execute(
            select(*cols)
            .where(
                ChatFile.conversation_id == conversation_id,
                ChatFile.included_in_context.is_(True),
            )
            .order_by(ChatFile.created_at.asc())
        )
    ).all()
    by_name: dict[str, dict] = {}
    for row in rows:
        base = os.path.basename(row.filename or "")
        if not base:
            continue
        entry: dict = {"filename": base, "id": row.id}
        if with_data:
            entry["media_type"] = row.media_type
            entry["base64"] = row.base64_data
        by_name[base] = entry  # latest by created_at wins
    return list(by_name.values())


def _seed_hash_of(winning: list[dict]) -> str | None:
    """sha256 over the sorted ``basename\\x00id`` pairs of the winning rows, or ``None`` for
    an empty set. Changes iff the seeded ``(basename -> row id)`` set changes."""
    if not winning:
        return None
    payload = "\n".join(sorted(f"{w['filename']}\x00{w['id']}" for w in winning))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def included_files_for_sandbox(
    db: AsyncSession, conversation_id: uuid.UUID
) -> list[dict]:
    """The conversation's included chat_files as sandbox seed inputs (Phase 5.4, tr-an0g.4):
    ``[{filename, base64, media_type}]``, basename-deduped (latest wins). The manager writes
    each into workdir/inputs/<filename> before the cell, so the model reads ``inputs/<filename>``."""
    winning = await _included_winning(db, conversation_id, with_data=True)
    return [
        {"filename": w["filename"], "base64": w["base64"], "media_type": w["media_type"]}
        for w in winning
    ]


async def included_files_seed_hash(
    db: AsyncSession, conversation_id: uuid.UUID
) -> str | None:
    """Cheap metadata-hash (NO base64) of the seeded set for the B3 probe (tr-f63e): the
    manager compares it to ``Kernel._seeded_hash`` to skip re-seeding unchanged uploads.
    ``None`` when there is nothing to seed."""
    return _seed_hash_of(await _included_winning(db, conversation_id, with_data=False))


async def included_files_for_sandbox_with_hash(
    db: AsyncSession, conversation_id: uuid.UUID
) -> tuple[list[dict], str | None]:
    """Full seed bytes AND the matching hash from ONE snapshot — used on a probe-miss retry so
    the bytes and the tag can never disagree (audit M2: no TOCTOU vs a separate hash query)."""
    winning = await _included_winning(db, conversation_id, with_data=True)
    files = [
        {"filename": w["filename"], "base64": w["base64"], "media_type": w["media_type"]}
        for w in winning
    ]
    return files, _seed_hash_of(winning)


async def copy_files(
    db: AsyncSession,
    src_message_id: uuid.UUID,
    dst_message_id: uuid.UUID,
) -> int:
    """Duplicate every chat_files row from src_message_id onto dst_message_id.

    Mirrors chat_image_service.copy_images for the edit-resend flow: the edited
    user message is a new node, so its files are cloned (rows copied, storage not
    shared — design §10). Added without committing; caller commits. Returns count.
    """
    src_rows = (
        await db.execute(
            select(ChatFile).where(ChatFile.message_id == src_message_id)
        )
    ).scalars().all()
    for row in src_rows:
        db.add(
            ChatFile(
                user_id=row.user_id,
                conversation_id=row.conversation_id,
                message_id=dst_message_id,
                filename=row.filename,
                media_type=row.media_type,
                kind=row.kind,
                base64_data=row.base64_data,
                file_size_bytes=row.file_size_bytes,
                token_estimate=row.token_estimate,
                # Carry the toggle so excluding a file then editing the message
                # doesn't silently re-include the clone (tr-kx4f).
                included_in_context=row.included_in_context,
            )
        )
    return len(src_rows)


async def files_by_message(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    excluded_file_ids: "frozenset[str] | None" = None,
) -> dict[uuid.UUID, list[dict]]:
    """Persisted, non-excluded files grouped by their owning message_id.

    Each value is a list of raw `file` content parts ready to re-inject into a
    historical user message. Empty dict when the conversation has no stored files,
    keeping history byte-identical to the text-only/image-only path.
    """
    excluded = excluded_file_ids or frozenset()
    rows = (
        await db.execute(
            select(
                ChatFile.message_id,
                ChatFile.filename,
                ChatFile.media_type,
                ChatFile.base64_data,
                ChatFile.id,
            )
            # included_in_context = false means the user excluded this file via the
            # "в контексте" toggle — skip it so it is NOT re-injected on later
            # turns (tr-kx4f).
            .where(
                ChatFile.conversation_id == conversation_id,
                ChatFile.included_in_context.is_(True),
            )
            .order_by(ChatFile.created_at.asc())
        )
    ).all()

    by_message: dict[uuid.UUID, list[dict]] = {}
    for msg_id, filename, media_type, base64_data, file_id in rows:
        if msg_id is None or str(file_id) in excluded:
            continue
        by_message.setdefault(msg_id, []).append(
            {
                "type": "file",
                "file": {
                    "filename": filename,
                    "file_data": f"data:{media_type};base64,{base64_data}",
                },
            }
        )
    return by_message
