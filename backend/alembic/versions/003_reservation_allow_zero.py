"""allow zero reservations for free models

Free models (e.g. `*:free`) have zero input/output pricing, so their estimated
cost — and the amount reserved before the OpenRouter call — is legitimately 0.
The original `reserved_micro_rub > 0` CHECK rejected every free-model request at
the reservation INSERT, which surfaced as an empty assistant bubble (the SSE
stream closed before emitting any event). Relax the check to `>= 0`.

Revision ID: 003
Revises: 002
Create Date: 2026-05-21
"""
from __future__ import annotations

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None

_CONSTRAINT = "chk_reservations_reserved_positive"
_TABLE = "reservations"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, "reserved_micro_rub >= 0")


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, "reserved_micro_rub > 0")
