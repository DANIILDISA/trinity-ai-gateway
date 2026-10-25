"""PostgreSQL enum type declarations.

These are declared with `create_type=False` because they are created explicitly
in the initial migration. Models reference these instances so the ORM knows the
column types without trying to re-create them.
"""
from sqlalchemy.dialects.postgresql import ENUM

transaction_type_enum = ENUM(
    "credit",
    "debit",
    "refund",
    "adjustment",
    name="transaction_type",
    create_type=False,
)

reservation_status_enum = ENUM(
    "active",
    "finalized",
    "released",
    "expired",
    name="reservation_status",
    create_type=False,
)

message_role_enum = ENUM(
    "user",
    "assistant",
    "system",
    name="message_role",
    create_type=False,
)

alert_severity_enum = ENUM(
    "critical",
    "warning",
    "info",
    name="alert_severity",
    create_type=False,
)

alert_status_enum = ENUM(
    "new",
    "acknowledged",
    "resolved",
    name="alert_status",
    create_type=False,
)

notification_channel_enum = ENUM(
    "in_app",
    "email",
    "both",
    name="notification_channel",
    create_type=False,
)
