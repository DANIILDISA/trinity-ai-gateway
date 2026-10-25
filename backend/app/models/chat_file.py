from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ChatFile(Base):
    """A persisted non-image file attached to a user message.

    Mirrors ChatImage: the file's payload is kept verbatim so it can be shown in
    the Files view, opened/downloaded from chat history, and re-injected into the
    model context on later turns. `base64_data` is raw base64 (no data-URI
    prefix): for text/code files it is the UTF-8 text encoded as base64, for
    PDF/DOCX it is the raw bytes (matches the wire `file` part). `kind` is one of
    text|pdf|docx (frontend lib/fileTypes.ts). conversation_id / message_id are
    nullable + ON DELETE SET NULL so purging a chat never orphans the Files view.
    """

    __tablename__ = "chat_files"

    # Partial covering index for the B3 seed-hash hot-path query (tr-f63e): every run_python
    # cell filters included rows for a conversation, ordered by created_at; INCLUDE keeps the
    # cheap (no-base64) hash query index-only.
    __table_args__ = (
        Index(
            "ix_chat_files_conv_included_created",
            "conversation_id", "created_at",
            postgresql_where=text("included_in_context IS TRUE"),
            postgresql_include=["id", "filename"],
        ),
    )

    id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    message_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # text | pdf | docx — drives how the file is re-injected and rendered.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    base64_data: Mapped[str] = mapped_column(Text, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    # Server-computed at save time, mirroring the frontend's per-kind estimate so
    # the analytics panel can sum tokens without re-reading payloads.
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # The "в контексте" toggle: when false the user excluded this file, so the
    # context builder must NOT re-inject it on later turns. Persisted so the
    # choice survives reload (tr-kx4f). Defaults true to preserve the prior
    # implicit "everything is in context" behavior.
    included_in_context: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
