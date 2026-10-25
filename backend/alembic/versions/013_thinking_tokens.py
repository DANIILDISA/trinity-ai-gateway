"""add reasoning_tokens + thinking columns for extended thinking

Extended thinking (reasoning) support:
- usage_logs.reasoning_tokens: billing/admin telemetry. OpenRouter reports the
  reasoning portion via usage.completion_tokens_details.reasoning_tokens; it is
  ALREADY included in completion_tokens, so billing splits it out and charges it
  at the input rate (BILLING_LOGIC §8).
- messages.reasoning_tokens: per-message token split so reloads show it.
- messages.thinking: the model's thinking text, persisted so the collapsible
  "Thinking" block survives a page reload (Claude.ai pattern).

`model_pricing.supports_reasoning` already exists (migration 008) and is reused
as the capability gate — it is NOT re-added here.

Revision ID: 013
Revises: 012
Create Date: 2026-06-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "usage_logs",
        sa.Column(
            "reasoning_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "messages",
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column("thinking", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("messages", "thinking")
    op.drop_column("messages", "reasoning_tokens")
    op.drop_column("usage_logs", "reasoning_tokens")
