from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.enums import transaction_type_enum


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    type: Mapped[str] = mapped_column(transaction_type_enum, nullable=False)
    amount_micro_rub: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after_micro_rub: Mapped[int] = mapped_column(BigInteger, nullable=False)

    description: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[PyUUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    admin_user_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    admin_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_transactions_idempotency"),
        CheckConstraint(
            "type != 'credit' OR amount_micro_rub > 0", name="chk_credit_positive"
        ),
        CheckConstraint(
            "type != 'debit' OR amount_micro_rub < 0", name="chk_debit_negative"
        ),
        CheckConstraint(
            "type != 'refund' OR amount_micro_rub > 0", name="chk_refund_positive"
        ),
        CheckConstraint(
            "type != 'adjustment' OR admin_reason IS NOT NULL",
            name="chk_adjustment_has_reason",
        ),
    )
