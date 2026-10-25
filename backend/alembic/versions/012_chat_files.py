"""add chat_files table

Persists every non-image file a user sends so it can be shown in the Files view,
opened/downloaded from chat history, and re-injected into the model context on
later turns (parity with chat_images). Stores the raw base64 payload, a
server-computed token estimate, and the file kind (text|pdf|docx).

Revision ID: 012
Revises: 011
Create Date: 2026-05-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None

_TABLE = "chat_files"


def upgrade() -> None:
    op.create_table(
        _TABLE,
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
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("base64_data", sa.Text(), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "token_estimate", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_chat_files_user_created",
        _TABLE,
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index("ix_chat_files_conversation", _TABLE, ["conversation_id"])
    op.create_index("ix_chat_files_message", _TABLE, ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_files_message", table_name=_TABLE)
    op.drop_index("ix_chat_files_conversation", table_name=_TABLE)
    op.drop_index("ix_chat_files_user_created", table_name=_TABLE)
    op.drop_table(_TABLE)
