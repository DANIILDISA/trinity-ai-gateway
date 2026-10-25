"""add model capability flags to model_pricing

Five boolean flags derived from the OpenRouter /models response (input
modalities, supported parameters, web-search pricing). They drive the
capability badges shown in the model picker. All default to false so existing
rows remain valid until the next catalogue sync repopulates them.

Revision ID: 008
Revises: 007
Create Date: 2026-05-23
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None

_TABLE = "model_pricing"
_COLUMNS = (
    "supports_image_input",
    "supports_file_input",
    "supports_tools",
    "supports_reasoning",
    "supports_web_search",
)


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            _TABLE,
            sa.Column(
                name,
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column(_TABLE, name)
