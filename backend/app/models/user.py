from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import BigInteger, CheckConstraint, Computed, DateTime, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    email_lower: Mapped[str] = mapped_column(
        String(255), Computed("lower(email)", persisted=True), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    yandex_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'user'"))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'active'")
    )
    block_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    balance_micro_rub: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    reserved_micro_rub: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    # tr-9wq: denormalized per-user artifact bytes, maintained by the
    # artifacts_user_bytes DB trigger (migration 018). Read by _user_total_bytes for
    # the quota check. NEVER write from app code — the trigger owns it.
    artifact_bytes_used: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduled_delete_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("email_lower", name="uq_users_email_lower"),
        UniqueConstraint("yandex_id", name="uq_users_yandex_id"),
        CheckConstraint("role IN ('user', 'admin')", name="chk_users_role"),
        CheckConstraint("status IN ('active', 'blocked')", name="chk_users_status"),
        CheckConstraint("balance_micro_rub >= 0", name="chk_users_balance_nonneg"),
        CheckConstraint("reserved_micro_rub >= 0", name="chk_users_reserved_nonneg"),
        CheckConstraint(
            "balance_micro_rub >= reserved_micro_rub", name="chk_available_balance"
        ),
    )
