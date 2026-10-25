"""add account grouping + blocking to api_keys

Future-proofs for multiple OpenRouter accounts each holding multiple keys.
account_name groups keys; account_blocked lets an admin disqualify every key
of a banned account from being selected as the primary, in one action.

Revision ID: 007
Revises: 006
Create Date: 2026-05-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None

_TABLE = "api_keys"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("account_name", sa.String(length=100), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "account_blocked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "account_blocked")
    op.drop_column(_TABLE, "account_name")
