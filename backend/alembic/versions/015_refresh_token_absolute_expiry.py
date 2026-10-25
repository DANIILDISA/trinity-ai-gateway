"""refresh_tokens.absolute_expires_at — sliding 7d window + absolute 14d cap

Old scheme: expires_at = login + 7d, fixed (rotation inherited it verbatim).
New scheme: expires_at slides to now + 7d on every refresh, clamped to
absolute_expires_at = login + 14d.

Backfill: old rows always had expires_at = login + 7d, so
absolute_expires_at = expires_at + interval '7 days' reconstructs exactly
login + 14d — existing sessions get the new policy as if it had applied
since their login. The interval is hardcoded (migrations must not read
runtime settings); it matches the pre-change jwt_refresh_token_expire_days=7.

Revision ID: 015
Revises: 014
Create Date: 2026-06-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "refresh_tokens",
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE refresh_tokens "
        "SET absolute_expires_at = expires_at + interval '7 days'"
    )
    op.alter_column("refresh_tokens", "absolute_expires_at", nullable=False)


def downgrade() -> None:
    op.drop_column("refresh_tokens", "absolute_expires_at")
