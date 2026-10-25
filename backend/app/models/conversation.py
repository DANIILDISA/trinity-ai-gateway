from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # Last-used generation posture (migration 020), restored when the chat is
    # reopened. NULL on legacy rows / before the first send → frontend defaults.
    last_thinking_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_thinking_effort: Mapped[str | None] = mapped_column(String(10), nullable=True)
    last_tools_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_web_search_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Denormalized tail of the active root->leaf path (migration 014). NULL for
    # legacy rows -> read path falls back to created_at ordering. ON DELETE
    # SET NULL so deleting the leaf message doesn't orphan the conversation.
    active_leaf_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    # tr-9wq: denormalized «Все ветки» totals, maintained by the messages_branch_totals
    # DB trigger (migration 018). Read by _compute_branch_totals instead of a 6-way
    # SUM/COUNT on every conversation GET. NEVER write these from app code — the trigger
    # owns them.
    bt_total_cost_micro_rub: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    bt_total_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    bt_total_output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    bt_total_reasoning_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    bt_message_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    bt_generation_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
