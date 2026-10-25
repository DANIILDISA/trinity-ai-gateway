"""add artifacts table + messages.tool_steps for agentic tool-use

Foundation for Sub-project 1 (engine + file tools):
- `artifacts`: model-authored files written by the file tools (create_file /
  str_replace), versioned within a `group_id` (a str_replace writes a new
  version row; the latest version is the current file). Distinct from
  user-uploaded `chat_files`. FK conventions mirror chat_files (user CASCADE,
  conversation/message SET NULL) so purging a chat never orphans-crashes.
- `messages.tool_steps` JSONB: the persisted live tool-step timeline
  `[{id, tool, title, filename?, diff?, presented?, status, output?}]`, restored
  on reload / post-Stop reconcile like `thinking_segments`. NULL when no tools ran.

Revision ID: 017
Revises: 016
Create Date: 2026-06-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
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
        # Artifacts are conversation outputs: deleting the conversation deletes
        # them (CASCADE) so there are no orphans inflating the per-user quota.
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Backstop against two concurrent sessions inserting the same version of
        # one file (the executor also takes SELECT … FOR UPDATE on the group).
        sa.UniqueConstraint(
            "conversation_id", "group_id", "version",
            name="uq_artifacts_conv_group_version",
        ),
    )
    op.create_index("ix_artifacts_user_id", "artifacts", ["user_id"])
    op.create_index(
        "ix_artifacts_conv_group_version",
        "artifacts",
        ["conversation_id", "group_id", "version"],
    )
    op.add_column(
        "messages",
        sa.Column(
            "tool_steps", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("messages", "tool_steps")
    op.drop_index("ix_artifacts_conv_group_version", table_name="artifacts")
    op.drop_index("ix_artifacts_user_id", table_name="artifacts")
    op.drop_table("artifacts")
