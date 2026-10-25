from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class UsageLog(Base):
    __tablename__ = "usage_logs"

    id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    request_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)

    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    # Reasoning/thinking tokens (a subset of output_tokens as reported by
    # OpenRouter). Billed at the input rate (BILLING_LOGIC §8); 0 when thinking
    # was off or unsupported.
    reasoning_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    estimated_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)

    estimated_cost_micro_rub: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_cost_micro_rub: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_micro_rub: Mapped[int] = mapped_column(BigInteger, nullable=False)

    overdraft_absorbed_micro_rub: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )

    openrouter_cost_micro_usd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # True when this completed row's OpenRouter cost was NOT captured from the
    # stream usage chunk (cost_usd was absent) and is awaiting backfill via the
    # /generation lookup. Distinguishes "cost unknown" from a genuine $0 so the
    # backfill cron and the dashboard margin can treat them differently (tr-34mj).
    cost_pending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    exchange_rate_micro_rub: Mapped[int] = mapped_column(BigInteger, nullable=False)

    estimation_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reservation_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    openrouter_ttfb_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    openrouter_total_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finalization_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'completed'")
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Length of the user's prompt in characters, and when the request finished
    # (success/partial). Both feed the admin request-tracking view (FEATURE 1).
    prompt_length_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    web_search_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    openrouter_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    api_key_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_keys.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("request_id", name="uq_usage_logs_request_id"),
        CheckConstraint(
            "status IN ('completed', 'partial', 'failed', 'refunded')",
            name="chk_usage_logs_status",
        ),
    )
