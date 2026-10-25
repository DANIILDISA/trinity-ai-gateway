from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import message_role_enum


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    conversation_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(message_role_enum, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Reasoning portion of output_tokens (extended thinking); NULL for messages
    # produced without thinking.
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_micro_rub: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # The model's thinking text, persisted so the collapsible "Thinking" block
    # survives a reload (Claude.ai pattern). NULL when thinking was off.
    thinking: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Ordered reasoning segments [{id, text, summary}] for the multi-segment
    # thinking timeline (per-segment titles + collapse); survives reload + post-
    # Stop reconcile (tr-cy0.3.7). NULL when thinking was off, for a single
    # segment, or for legacy rows — all of which fall back to `thinking`.
    thinking_segments: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Ordered agentic tool steps [{id, tool, title, filename?, diff?, presented?,
    # status, output?}] streamed live into the «Размышления» timeline and
    # persisted for reload / post-Stop reconcile (tr-cy0.2.7). NULL when no tools ran.
    tool_steps: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    request_id: Mapped[PyUUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # --- Version history / branching (migration 014) ---
    # Self-FK: siblings sharing the same (parent_id, role) are versions of one
    # turn. NULL for the first turn / legacy rows. CASCADE only fires on
    # conversation teardown; user-facing removal uses deleted_at.
    parent_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Marks the rendered root->leaf path; every history/context read filters on
    # `is_active = true`. Defaults true so legacy rows stay on the path.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    # Soft-delete parity with conversations; hides abandoned/partial nodes
    # without breaking request_id <-> usage_log reconciliation.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Self-referential: parent.children = sibling versions/continuations.
    # remote_side pins Message.id as the "one" end so SQLAlchemy resolves the
    # direction of the self-join.
    parent: Mapped["Message | None"] = relationship(
        "Message",
        remote_side="Message.id",
        back_populates="children",
    )
    children: Mapped[list["Message"]] = relationship(
        "Message",
        back_populates="parent",
    )
