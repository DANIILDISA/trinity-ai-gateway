"""allow 'unavailable' model_pricing status for automatic sync

The automatic OpenRouter model sync marks models that disappear from the
upstream /models response as status='unavailable' (rather than deleting their
pricing history). The original CHECK constraint only permitted
('available', 'disabled', 'deprecated'), so it must be widened.

No new columns are required — only the status CHECK constraint changes.

Revision ID: 005
Revises: 004
Create Date: 2026-05-22
"""
from __future__ import annotations

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None

_CONSTRAINT = "chk_model_pricing_status"
_TABLE = "model_pricing"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        "status IN ('available', 'disabled', 'deprecated', 'unavailable')",
    )


def downgrade() -> None:
    # Collapse any 'unavailable' rows back to 'disabled' so the narrower
    # constraint can be reinstated without violation.
    op.execute(
        "UPDATE model_pricing SET status = 'disabled' "
        "WHERE status = 'unavailable'"
    )
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        "status IN ('available', 'disabled', 'deprecated')",
    )
