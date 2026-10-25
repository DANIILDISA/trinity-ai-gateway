"""add user full_name and scheduled_delete_at

Adds two profile/account-lifecycle columns to ``users``:

- ``full_name`` — optional polite-form full name shown in settings (separate
  from ``display_name`` which is the chat handle).
- ``scheduled_delete_at`` — soft-delete marker. When set, the user is treated
  as pending deletion: login fails with ``account_pending_deletion`` and the
  recovery endpoint can clear the flag on a valid password re-entry.

A partial index on ``scheduled_delete_at`` keeps the sweep query (future
hard-delete job) cheap while costing nothing for active rows.

Revision ID: 011
Revises: 010
Create Date: 2026-05-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("full_name", sa.String(length=150), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "scheduled_delete_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_users_scheduled_delete_at",
        "users",
        ["scheduled_delete_at"],
        postgresql_where=sa.text("scheduled_delete_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_users_scheduled_delete_at", table_name="users")
    op.drop_column("users", "scheduled_delete_at")
    op.drop_column("users", "full_name")
