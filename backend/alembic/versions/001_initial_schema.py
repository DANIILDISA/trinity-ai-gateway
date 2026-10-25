"""initial schema

Creates all enums, tables, indexes, and seeds system_settings as defined in
SCHEMA.md §3 (tables), §4 (indexes), and §7.4 (seed data). Tables are created
in dependency order; downgrade drops them in reverse.

Revision ID: 001
Revises:
Create Date: 2026-05-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


# --------------------------------------------------------------------------- #
# Enum definitions — created explicitly so the same instances can be reused in
# columns with create_type=False (avoids "type already exists" on table create).
# --------------------------------------------------------------------------- #

transaction_type_enum = postgresql.ENUM(
    "credit", "debit", "refund", "adjustment", name="transaction_type"
)
reservation_status_enum = postgresql.ENUM(
    "active", "finalized", "released", "expired", name="reservation_status"
)
message_role_enum = postgresql.ENUM(
    "user", "assistant", "system", name="message_role"
)
alert_severity_enum = postgresql.ENUM(
    "critical", "warning", "info", name="alert_severity"
)
alert_status_enum = postgresql.ENUM(
    "new", "acknowledged", "resolved", name="alert_status"
)
notification_channel_enum = postgresql.ENUM(
    "in_app", "email", "both", name="notification_channel"
)


def _enum_ref(enum: postgresql.ENUM) -> postgresql.ENUM:
    """Return a non-creating reference to an already-created enum type."""
    return postgresql.ENUM(*enum.enums, name=enum.name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()

    # 1) Create enums --------------------------------------------------------
    transaction_type_enum.create(bind, checkfirst=False)
    reservation_status_enum.create(bind, checkfirst=False)
    message_role_enum.create(bind, checkfirst=False)
    alert_severity_enum.create(bind, checkfirst=False)
    alert_status_enum.create(bind, checkfirst=False)
    notification_channel_enum.create(bind, checkfirst=False)

    # 2) users ---------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column(
            "email_lower",
            sa.String(255),
            sa.Computed("lower(email)", persisted=True),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column("yandex_id", sa.String(64), nullable=True),
        sa.Column(
            "role",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'user'"),
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("block_reason", sa.Text, nullable=True),
        sa.Column(
            "balance_micro_rub",
            sa.BigInteger,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "reserved_micro_rub",
            sa.BigInteger,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("email_lower", name="uq_users_email_lower"),
        sa.UniqueConstraint("yandex_id", name="uq_users_yandex_id"),
        sa.CheckConstraint("role IN ('user', 'admin')", name="chk_users_role"),
        sa.CheckConstraint(
            "status IN ('active', 'blocked')", name="chk_users_status"
        ),
        sa.CheckConstraint("balance_micro_rub >= 0", name="chk_users_balance_nonneg"),
        sa.CheckConstraint(
            "reserved_micro_rub >= 0", name="chk_users_reserved_nonneg"
        ),
        sa.CheckConstraint(
            "balance_micro_rub >= reserved_micro_rub", name="chk_available_balance"
        ),
    )

    # 3) refresh_tokens ------------------------------------------------------
    op.create_table(
        "refresh_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("ip_address", postgresql.INET, nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_refresh_token_hash"),
    )

    # 4) password_reset_tokens ----------------------------------------------
    op.create_table(
        "password_reset_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("token_hash", name="uq_password_reset_token_hash"),
    )

    # 5) api_keys ------------------------------------------------------------
    op.create_table(
        "api_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("encrypted_key", sa.Text, nullable=False),
        sa.Column("key_suffix", sa.String(8), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "is_primary",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "total_requests",
            sa.BigInteger,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "total_cost_micro_usd",
            sa.BigInteger,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'exhausted')",
            name="chk_api_keys_status",
        ),
    )

    # 6) system_settings -----------------------------------------------------
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", postgresql.JSONB, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
    )

    # 7) model_pricing -------------------------------------------------------
    op.create_table(
        "model_pricing",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("model_name", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("openrouter_input_price_micro_usd", sa.BigInteger, nullable=False),
        sa.Column("openrouter_output_price_micro_usd", sa.BigInteger, nullable=False),
        sa.Column("input_price_micro_rub", sa.BigInteger, nullable=False),
        sa.Column("output_price_micro_rub", sa.BigInteger, nullable=False),
        sa.Column(
            "is_price_override",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("max_context_tokens", sa.Integer, nullable=False),
        sa.Column("max_output_tokens", sa.Integer, nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'available'"),
        ),
        sa.Column(
            "is_current",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "effective_from",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("effective_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('available', 'disabled', 'deprecated')",
            name="chk_model_pricing_status",
        ),
    )

    # 8) transactions --------------------------------------------------------
    op.create_table(
        "transactions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("type", _enum_ref(transaction_type_enum), nullable=False),
        sa.Column("amount_micro_rub", sa.BigInteger, nullable=False),
        sa.Column("balance_after_micro_rub", sa.BigInteger, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payment_id", sa.String(128), nullable=True),
        sa.Column(
            "admin_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("admin_reason", sa.Text, nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_transactions_idempotency"),
        sa.CheckConstraint(
            "type != 'credit' OR amount_micro_rub > 0", name="chk_credit_positive"
        ),
        sa.CheckConstraint(
            "type != 'debit' OR amount_micro_rub < 0", name="chk_debit_negative"
        ),
        sa.CheckConstraint(
            "type != 'refund' OR amount_micro_rub > 0", name="chk_refund_positive"
        ),
        sa.CheckConstraint(
            "type != 'adjustment' OR admin_reason IS NOT NULL",
            name="chk_adjustment_has_reason",
        ),
    )

    # 9) reservations --------------------------------------------------------
    op.create_table(
        "reservations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            _enum_ref(reservation_status_enum),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("reserved_micro_rub", sa.BigInteger, nullable=False),
        sa.Column("actual_micro_rub", sa.BigInteger, nullable=True),
        sa.Column("refunded_micro_rub", sa.BigInteger, nullable=True),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("estimated_input_tokens", sa.Integer, nullable=False),
        sa.Column("estimated_output_tokens", sa.Integer, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("request_id", name="uq_reservations_request_id"),
        sa.CheckConstraint(
            "reserved_micro_rub > 0", name="chk_reservations_reserved_positive"
        ),
    )

    # 10) conversations ------------------------------------------------------
    op.create_table(
        "conversations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 11) messages -----------------------------------------------------------
    op.create_table(
        "messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", _enum_ref(message_role_enum), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("model_id", sa.String(128), nullable=True),
        sa.Column("input_tokens", sa.Integer, nullable=True),
        sa.Column("output_tokens", sa.Integer, nullable=True),
        sa.Column("cost_micro_rub", sa.BigInteger, nullable=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # 12) usage_logs ---------------------------------------------------------
    op.create_table(
        "usage_logs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False),
        sa.Column("output_tokens", sa.Integer, nullable=False),
        sa.Column("estimated_input_tokens", sa.Integer, nullable=False),
        sa.Column("estimated_output_tokens", sa.Integer, nullable=False),
        sa.Column("estimated_cost_micro_rub", sa.BigInteger, nullable=False),
        sa.Column("actual_cost_micro_rub", sa.BigInteger, nullable=False),
        sa.Column("reserved_micro_rub", sa.BigInteger, nullable=False),
        sa.Column(
            "overdraft_absorbed_micro_rub",
            sa.BigInteger,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("openrouter_cost_micro_usd", sa.BigInteger, nullable=False),
        sa.Column("exchange_rate_micro_rub", sa.BigInteger, nullable=False),
        sa.Column("estimation_ms", sa.Integer, nullable=True),
        sa.Column("reservation_ms", sa.Integer, nullable=True),
        sa.Column("openrouter_ttfb_ms", sa.Integer, nullable=True),
        sa.Column("openrouter_total_ms", sa.Integer, nullable=True),
        sa.Column("finalization_ms", sa.Integer, nullable=True),
        sa.Column("total_ms", sa.Integer, nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'completed'"),
        ),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("openrouter_request_id", sa.String(128), nullable=True),
        sa.Column(
            "api_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_keys.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("request_id", name="uq_usage_logs_request_id"),
        sa.CheckConstraint(
            "status IN ('completed', 'partial', 'failed', 'refunded')",
            name="chk_usage_logs_status",
        ),
    )

    # 13) admin_audit_log ----------------------------------------------------
    op.create_table(
        "admin_audit_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("actor_email", sa.String(255), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=True),
        sa.Column("target_id", sa.String(255), nullable=True),
        sa.Column("details", postgresql.JSONB, nullable=True),
        sa.Column("ip_address", postgresql.INET, nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # 14) alerts -------------------------------------------------------------
    op.create_table(
        "alerts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("severity", _enum_ref(alert_severity_enum), nullable=False),
        sa.Column(
            "status",
            _enum_ref(alert_status_enum),
            nullable=False,
            server_default=sa.text("'new'"),
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("details", postgresql.JSONB, nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "acknowledged_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "resolved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # 15) notifications ------------------------------------------------------
    op.create_table(
        "notifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column(
            "channel",
            _enum_ref(notification_channel_enum),
            nullable=False,
            server_default=sa.text("'in_app'"),
        ),
        sa.Column(
            "is_read",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "email_sent",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("email_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # 16) Indexes (SCHEMA.md §4) --------------------------------------------
    op.create_index(
        "idx_users_last_active",
        "users",
        [sa.text("last_active_at DESC NULLS LAST")],
    )
    op.create_index(
        "idx_users_balance",
        "users",
        [sa.text("balance_micro_rub DESC")],
    )

    op.create_index("idx_refresh_tokens_expires", "refresh_tokens", ["expires_at"])
    op.create_index("idx_refresh_tokens_user", "refresh_tokens", ["user_id"])

    op.create_index(
        "idx_password_reset_expires", "password_reset_tokens", ["expires_at"]
    )

    op.create_index(
        "idx_transactions_user_created",
        "transactions",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_transactions_type_created",
        "transactions",
        ["type", "created_at"],
    )
    op.create_index(
        "idx_transactions_payment_id",
        "transactions",
        ["payment_id"],
        postgresql_where=sa.text("payment_id IS NOT NULL"),
    )

    op.create_index(
        "idx_reservations_user_active",
        "reservations",
        ["user_id"],
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "idx_reservations_active_created",
        "reservations",
        ["created_at"],
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_index(
        "idx_conversations_user_active",
        "conversations",
        ["user_id", sa.text("updated_at DESC")],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_index(
        "idx_messages_conversation_created",
        "messages",
        ["conversation_id", "created_at"],
    )

    op.create_index(
        "idx_usage_logs_model_created",
        "usage_logs",
        ["model_id", "created_at"],
    )
    op.create_index(
        "idx_usage_logs_user_created",
        "usage_logs",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_usage_logs_status_created",
        "usage_logs",
        ["status", "created_at"],
    )

    op.create_index(
        "idx_model_pricing_current",
        "model_pricing",
        ["model_id"],
        postgresql_where=sa.text("is_current = true"),
    )
    op.create_index(
        "idx_model_pricing_history",
        "model_pricing",
        ["model_id", "effective_from"],
    )

    op.create_index(
        "idx_audit_log_created",
        "admin_audit_log",
        [sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_audit_log_action",
        "admin_audit_log",
        ["action", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_audit_log_actor",
        "admin_audit_log",
        ["actor_id", sa.text("created_at DESC")],
        postgresql_where=sa.text("actor_id IS NOT NULL"),
    )
    op.create_index(
        "idx_audit_log_target",
        "admin_audit_log",
        ["target_type", "target_id", sa.text("created_at DESC")],
        postgresql_where=sa.text("target_type IS NOT NULL"),
    )

    op.create_index(
        "idx_alerts_status_created",
        "alerts",
        ["status", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_alerts_severity",
        "alerts",
        ["severity", sa.text("created_at DESC")],
    )

    op.create_index(
        "idx_notifications_user_unread",
        "notifications",
        ["user_id", sa.text("created_at DESC")],
        postgresql_where=sa.text("is_read = false"),
    )
    op.create_index(
        "idx_notifications_user_created",
        "notifications",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_notifications_email_pending",
        "notifications",
        ["created_at"],
        postgresql_where=sa.text(
            "channel IN ('email', 'both') AND email_sent = false"
        ),
    )

    op.create_index(
        "idx_api_keys_primary",
        "api_keys",
        ["is_primary"],
        postgresql_where=sa.text("is_primary = true AND status = 'active'"),
    )

    # 17) Seed system_settings (SCHEMA.md §7.4) -----------------------------
    op.execute(
        sa.text(
            """
            INSERT INTO system_settings (key, value, description) VALUES
                ('exchange_rate_micro_rub',          '82500000'::jsonb,                'USD/RUB exchange rate in micro-rubles per 1 USD'),
                ('exchange_rate_updated_at',         '"2026-04-29T00:00:00Z"'::jsonb,  'When the exchange rate was last updated'),
                ('exchange_rate_cooldown_days',      '7'::jsonb,                       'Minimum days between exchange rate updates'),
                ('markup_multiplier',                '1.25'::jsonb,                    'Multiplier over OpenRouter cost (1.25 = 25% markup)'),
                ('rate_limit_per_minute',            '10'::jsonb,                      'Max requests per user per minute'),
                ('rate_limit_per_hour',              '100'::jsonb,                     'Max requests per user per hour'),
                ('rate_limit_per_day',               '500'::jsonb,                     'Max requests per user per day'),
                ('openrouter_balance_threshold_usd', '100'::jsonb,                     'Alert when OpenRouter balance below this USD amount'),
                ('low_balance_remaining_requests',   '5'::jsonb,                       'Warn user when balance covers fewer than N avg requests'),
                ('min_topup_amount_rub',             '500'::jsonb,                     'Minimum top-up amount in whole rubles'),
                ('reservation_buffer_multiplier',    '2.0'::jsonb,                     'Reserve N x estimated cost'),
                ('overdraft_tolerance_percent',      '7'::jsonb,                       'Allowed overdraft as % of reservation'),
                ('reconciliation_threshold_percent', '2'::jsonb,                       'Alert if reconciliation discrepancy exceeds this %');
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()

    # Drop in reverse dependency order. Indexes drop with their tables.
    op.drop_table("notifications")
    op.drop_table("alerts")
    op.drop_table("admin_audit_log")
    op.drop_table("usage_logs")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("reservations")
    op.drop_table("transactions")
    op.drop_table("model_pricing")
    op.drop_table("system_settings")
    op.drop_table("api_keys")
    op.drop_table("password_reset_tokens")
    op.drop_table("refresh_tokens")
    op.drop_table("users")

    # Drop enums last (after all tables that reference them are gone)
    notification_channel_enum.drop(bind, checkfirst=False)
    alert_status_enum.drop(bind, checkfirst=False)
    alert_severity_enum.drop(bind, checkfirst=False)
    message_role_enum.drop(bind, checkfirst=False)
    reservation_status_enum.drop(bind, checkfirst=False)
    transaction_type_enum.drop(bind, checkfirst=False)
