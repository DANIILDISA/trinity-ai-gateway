"""add usage_logs tracking fields: prompt_length_chars, completed_at

Supports the admin request-tracking view (FEATURE 1). Both are nullable so
existing rows are unaffected; new rows populate them at finalization.

Revision ID: 004
Revises: 003
Create Date: 2026-05-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "usage_logs",
        sa.Column("prompt_length_chars", sa.Integer(), nullable=True),
    )
    op.add_column(
        "usage_logs",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("usage_logs", "completed_at")
    op.drop_column("usage_logs", "prompt_length_chars")
