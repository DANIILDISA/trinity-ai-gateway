"""message version history: branching columns on messages + conversations

Pure-additive, no data backfill (design §3-4). Adds:
- messages.parent_id   — self-FK (ON DELETE CASCADE). Siblings sharing the same
  (parent_id, role) are versions of one turn. NULL for the first turn / legacy rows.
- messages.is_active   — marks the rendered root->leaf path; every history/context
  read adds `AND is_active = true`. Defaults true so legacy rows stay on the path.
- messages.deleted_at  — soft-delete parity with conversations; hides
  partial-disconnect/abandoned nodes without breaking request_id<->usage_log recon.
- conversations.active_leaf_id — denormalized tail of the active path
  (ON DELETE SET NULL). NULL for legacy rows -> read path falls back to created_at.

Indexes: idx_messages_parent(parent_id) for sibling/child walks; partial
idx_messages_conv_active(conversation_id, created_at) WHERE is_active AND
deleted_at IS NULL for the active-path read. idx_messages_conversation_created
(migration 001) is kept untouched.

Revision ID: 014
Revises: 013
Create Date: 2026-06-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "messages",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_messages_parent",
        "messages",
        "messages",
        ["parent_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.add_column(
        "conversations",
        sa.Column("active_leaf_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_conversations_active_leaf",
        "conversations",
        "messages",
        ["active_leaf_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_index("idx_messages_parent", "messages", ["parent_id"])
    op.create_index(
        "idx_messages_conv_active",
        "messages",
        ["conversation_id", "created_at"],
        postgresql_where=sa.text("is_active AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_messages_conv_active", table_name="messages")
    op.drop_index("idx_messages_parent", table_name="messages")
    op.drop_constraint(
        "fk_conversations_active_leaf", "conversations", type_="foreignkey"
    )
    op.drop_column("conversations", "active_leaf_id")
    op.drop_constraint("fk_messages_parent", "messages", type_="foreignkey")
    op.drop_column("messages", "deleted_at")
    op.drop_column("messages", "is_active")
    op.drop_column("messages", "parent_id")
