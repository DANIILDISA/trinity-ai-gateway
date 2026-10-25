from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ModelPricing(Base):
    __tablename__ = "model_pricing"

    id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)

    openrouter_input_price_micro_usd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    openrouter_output_price_micro_usd: Mapped[int] = mapped_column(BigInteger, nullable=False)

    input_price_micro_rub: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_price_micro_rub: Mapped[int] = mapped_column(BigInteger, nullable=False)

    is_price_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    max_context_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Per-chunk streaming timeout (TTFB and inter-chunk silence). Raised to
    # several minutes for reasoning / deep-research models that think between
    # chunks; see model_sync_service for how it is derived.
    stream_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("30")
    )

    # Capability flags derived from the OpenRouter /models response. Refreshed on
    # every sync (capabilities can change upstream); see model_sync_service.
    supports_image_input: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    supports_file_input: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    supports_tools: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    supports_reasoning: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    supports_web_search: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'available'")
    )

    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    effective_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('available', 'disabled', 'deprecated', 'unavailable')",
            name="chk_model_pricing_status",
        ),
    )
