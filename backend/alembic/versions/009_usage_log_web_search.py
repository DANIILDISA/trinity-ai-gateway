"""add web_search_enabled to usage_logs

Records whether a request opted into the OpenRouter web-search plugin. The
per-search cost is already folded into the usage chunk's USD total, so this
column is purely informational (drives the chat UI indicator + analytics).

Revision ID: 009
Revises: 008
Create Date: 2026-05-23
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None

_TABLE = "usage_logs"
_COLUMN = "web_search_enabled"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
