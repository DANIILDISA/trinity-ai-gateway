"""chat_images/chat_files.included_in_context — persist the per-attachment "в контексте" toggle

ADDITIVE ONLY. Adds one column to BOTH `chat_images` and `chat_files`:

  - included_in_context  BOOLEAN  NOT NULL  DEFAULT true

The "в контексте" checkbox let the user exclude an attachment from the model
context, but the state lived only in client memory: the conversation GET DTO
omitted the field, so the frontend defaulted it back to `true` on every reload
(silent reset to checked), and — worse — the backend context builder re-injected
every attachment regardless, so an "excluded" image was still sent to the model
and re-billed for vision on each follow-up turn (tr-kx4f). Persisting the flag
lets the toggle survive reload AND lets the context builder skip excluded
attachments (cost-sensitive).

Existing rows default to true (the prior implicit "everything is in context"
behavior is preserved). Adding a column with a constant server_default is a
metadata-only change in PostgreSQL — no existing column, constraint, index, or
row is altered.

Revision ID: 022
Revises: 021
Create Date: 2026-06-24
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None

_TABLES = ("chat_images", "chat_files")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "included_in_context",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "included_in_context")
