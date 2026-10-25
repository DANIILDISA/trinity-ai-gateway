from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.enums import reservation_status_enum


class Reservation(Base):
    __tablename__ = "reservations"

    id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    request_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        reservation_status_enum, nullable=False, server_default=text("'active'")
    )

    reserved_micro_rub: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_micro_rub: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    refunded_micro_rub: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    estimated_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("request_id", name="uq_reservations_request_id"),
        # >= 0 (not > 0): free models have zero pricing, so their estimated
        # cost — and therefore the reserved amount — is legitimately 0. A strict
        # > 0 check rejected every free-model request at reservation insert.
        CheckConstraint("reserved_micro_rub >= 0", name="chk_reservations_reserved_positive"),
    )
