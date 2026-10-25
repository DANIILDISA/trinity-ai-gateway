"""add chat_images table

Persists every image a user sends so it can be shown in a gallery and
re-injected into multimodal context on later turns. Both a thumbnail and the
full base64 are stored; the gallery endpoint returns only the thumbnail, while
the conversation-context endpoint returns the full payload (SPEC: image
persistence feature).

Revision ID: 010
Revises: 009
Create Date: 2026-05-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None

_TABLE = "chat_images"


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
        sa.Column("thumbnail_base64", sa.Text(), nullable=False),
        sa.Column("full_base64", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(50), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_chat_images_user_created",
        _TABLE,
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index("ix_chat_images_conversation", _TABLE, ["conversation_id"])
    op.create_index("ix_chat_images_message", _TABLE, ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_images_message", table_name=_TABLE)
    op.drop_index("ix_chat_images_conversation", table_name=_TABLE)
    op.drop_index("ix_chat_images_user_created", table_name=_TABLE)
    op.drop_table(_TABLE)
