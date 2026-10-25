"""usage_logs.cost_pending — flag rows whose OpenRouter cost is UNKNOWN (not captured)

ADDITIVE ONLY. Adds one column to `usage_logs`:

  - cost_pending  BOOLEAN  NOT NULL  DEFAULT false

`openrouter_cost_micro_usd` is bigint NOT NULL, so a genuine $0 (free models,
demo traffic) and "cost was not captured" both stored 0 and were
indistinguishable — which made it impossible to backfill the right rows or to
exclude unknown-cost rows from the margin denominator (tr-34mj). `cost_pending`
separates the two states: it is set true when a completed request finalizes with
NO cost in the stream usage chunk (`usage.cost` absent, i.e. cost_usd is None).
The cost-backfill cron targets `cost_pending = true` rows and the dashboard
margin excludes them, while a real $0 (cost_pending = false) is kept.

Existing rows default to false (their captured cost is treated as known). No
existing column, constraint, index, or row is altered — adding a column with a
constant server_default is a metadata-only change in PostgreSQL.

Revision ID: 021
Revises: 020
Create Date: 2026-06-24
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "usage_logs",
        sa.Column(
            "cost_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("usage_logs", "cost_pending")
