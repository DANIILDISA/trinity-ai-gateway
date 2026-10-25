"""add per-model stream_timeout_seconds to model_pricing

Reasoning and deep-research models can think for minutes between SSE chunks,
exceeding the old hardcoded 30s per-chunk timeout. This adds a per-model
timeout (the per-chunk ceiling, not the total request timeout) so the sync
can grant such models a longer window while regular models keep 30s.

Revision ID: 006
Revises: 005
Create Date: 2026-05-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None

_TABLE = "model_pricing"
_COLUMN = "stream_timeout_seconds"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.Integer(),
            nullable=False,
            server_default=sa.text("30"),
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
