from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ChatImage(Base):
    """A persisted image attached to a user message.

    `thumbnail_base64` drives gallery/history display; `full_base64` is the
    payload re-injected into multimodal context on later turns. Both are stored
    as raw base64 (no data-URI prefix). conversation_id / message_id are
    nullable + ON DELETE SET NULL so purging a chat never orphans the gallery.
    """

    __tablename__ = "chat_images"

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

    thumbnail_base64: Mapped[str] = mapped_column(Text, nullable=False)
    full_base64: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(50), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # The "в контексте" toggle: when false the user excluded this image, so the
    # context builder must NOT re-inject it on later turns (avoids repeat vision
    # cost). Persisted so the choice survives reload (tr-kx4f). Defaults true to
    # preserve the prior implicit "everything is in context" behavior.
    included_in_context: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
