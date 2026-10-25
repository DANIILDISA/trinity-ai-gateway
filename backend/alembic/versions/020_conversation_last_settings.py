"""per-conversation last-used generation settings (restore on reopen)

ADDITIVE ONLY. Adds four NULLABLE columns to `conversations` so the UI can
restore the generation posture of the last request when a chat is reopened
(model is already on `conversations.model_id`; these cover the toggles):

  - last_thinking_enabled   BOOLEAN  NULL
  - last_thinking_effort    VARCHAR(10) NULL   ('low' | 'medium' | 'high')
  - last_tools_enabled      BOOLEAN  NULL
  - last_web_search_enabled BOOLEAN  NULL

No defaults / no backfill: existing rows keep NULL and the frontend falls back
to its normal defaults until the next send writes real values. No existing
column, constraint, index, or row is altered or dropped — adding a nullable
column is a metadata-only change in PostgreSQL, so no data is rewritten or lost.

Revision ID: 020
Revises: 019
Create Date: 2026-06-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("last_thinking_enabled", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("last_thinking_effort", sa.String(length=10), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("last_tools_enabled", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("last_web_search_enabled", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "last_web_search_enabled")
    op.drop_column("conversations", "last_tools_enabled")
    op.drop_column("conversations", "last_thinking_effort")
    op.drop_column("conversations", "last_thinking_enabled")
