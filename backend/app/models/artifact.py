from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Artifact(Base):
    """A model-authored file produced by the file tools (create_file / str_replace).

    Versioned within a `group_id`: a `str_replace` writes a NEW row with the same
    group_id and an incremented `version`; the latest version is the current file
    (the artifact card shows/downloads it). Versions are capped per group and the
    per-user quota counts ALL versions (file_tools). Distinct from user-uploaded
    `chat_files`. user CASCADE; conversation CASCADE (artifacts are conversation
    outputs — no orphans); message SET NULL (a soft-deleted message keeps it).
    """

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "group_id", "version",
            name="uq_artifacts_conv_group_version",
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
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=True,
    )
    message_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Stable id shared across all versions of one logical file.
    group_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
