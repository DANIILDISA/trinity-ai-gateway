"""add messages.thinking_segments JSONB for per-segment reasoning

Stores the ordered list of reasoning segments ``[{id, text, summary}]`` so the
multi-segment thinking timeline (per-segment titles + individual collapse) and
the post-Stop reconcile survive a reload (tr-cy0.3). The legacy ``thinking`` Text
column stays for backward compatibility: it remains the single-segment fallback,
and old rows keep ``thinking_segments`` NULL. NULL also when thinking was off or
the reply was a single segment (the client falls back to ``thinking``).

Revision ID: 016
Revises: 015
Create Date: 2026-06-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "thinking_segments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("messages", "thinking_segments")
