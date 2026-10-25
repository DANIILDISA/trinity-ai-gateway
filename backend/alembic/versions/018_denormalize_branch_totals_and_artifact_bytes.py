"""denormalize branch totals onto conversations + per-user artifact bytes onto
users (DB-trigger maintained), and add artifacts(conversation_id, filename) index

tr-9wq (audit §H3/H4/H5): removes the 6-way SUM/COUNT on every conversation GET
(_compute_branch_totals) and the SUM(size_bytes) on every file op (_user_total_bytes).
Counters are maintained by AFTER INSERT/UPDATE/DELETE row triggers so they can't
drift across any mutation path (app finalize, branching soft-delete, cron retention,
permanent-delete CASCADE, raw SQL). Assumes messages.conversation_id and
artifacts.user_id are immutable (no re-homing path exists).

Revision ID: 018
Revises: 017
Create Date: 2026-06-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None

_BT_COLS = [
    "bt_total_cost_micro_rub",
    "bt_total_input_tokens",
    "bt_total_output_tokens",
    "bt_total_reasoning_tokens",
    "bt_message_count",
    "bt_generation_count",
]

_MESSAGES_FN = """
CREATE OR REPLACE FUNCTION trg_messages_branch_totals() RETURNS trigger AS $$
DECLARE
    d_cost   bigint := 0;
    d_in     bigint := 0;
    d_out    bigint := 0;
    d_reason bigint := 0;
    d_msg    bigint := 0;
    d_gen    bigint := 0;
BEGIN
    IF (TG_OP = 'UPDATE' OR TG_OP = 'DELETE') AND OLD.deleted_at IS NULL THEN
        d_cost   := d_cost   - COALESCE(OLD.cost_micro_rub, 0);
        d_in     := d_in     - COALESCE(OLD.input_tokens, 0);
        d_out    := d_out    - COALESCE(OLD.output_tokens, 0);
        d_reason := d_reason - COALESCE(OLD.reasoning_tokens, 0);
        d_msg    := d_msg    - 1;
        d_gen    := d_gen    - (CASE WHEN OLD.role = 'assistant' THEN 1 ELSE 0 END);
    END IF;
    IF (TG_OP = 'INSERT' OR TG_OP = 'UPDATE') AND NEW.deleted_at IS NULL THEN
        d_cost   := d_cost   + COALESCE(NEW.cost_micro_rub, 0);
        d_in     := d_in     + COALESCE(NEW.input_tokens, 0);
        d_out    := d_out    + COALESCE(NEW.output_tokens, 0);
        d_reason := d_reason + COALESCE(NEW.reasoning_tokens, 0);
        d_msg    := d_msg    + 1;
        d_gen    := d_gen    + (CASE WHEN NEW.role = 'assistant' THEN 1 ELSE 0 END);
    END IF;
    IF d_cost <> 0 OR d_in <> 0 OR d_out <> 0 OR d_reason <> 0 OR d_msg <> 0 OR d_gen <> 0 THEN
        UPDATE conversations SET
            bt_total_cost_micro_rub   = bt_total_cost_micro_rub   + d_cost,
            bt_total_input_tokens     = bt_total_input_tokens     + d_in,
            bt_total_output_tokens    = bt_total_output_tokens    + d_out,
            bt_total_reasoning_tokens = bt_total_reasoning_tokens + d_reason,
            bt_message_count          = bt_message_count          + d_msg,
            bt_generation_count       = bt_generation_count       + d_gen
        WHERE id = COALESCE(NEW.conversation_id, OLD.conversation_id);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""

_ARTIFACTS_FN = """
CREATE OR REPLACE FUNCTION trg_artifacts_user_bytes() RETURNS trigger AS $$
DECLARE
    d_bytes bigint := 0;
BEGIN
    IF (TG_OP = 'UPDATE' OR TG_OP = 'DELETE') THEN
        d_bytes := d_bytes - COALESCE(OLD.size_bytes, 0);
    END IF;
    IF (TG_OP = 'INSERT' OR TG_OP = 'UPDATE') THEN
        d_bytes := d_bytes + COALESCE(NEW.size_bytes, 0);
    END IF;
    IF d_bytes <> 0 THEN
        UPDATE users SET artifact_bytes_used = artifact_bytes_used + d_bytes
        WHERE id = COALESCE(NEW.user_id, OLD.user_id);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    for col in _BT_COLS:
        op.add_column(
            "conversations",
            sa.Column(col, sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        )
    op.add_column(
        "users",
        sa.Column("artifact_bytes_used", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )
    op.create_index(
        "idx_artifacts_conv_filename",
        "artifacts",
        ["conversation_id", "filename", sa.text("version DESC")],
    )
    op.execute(_MESSAGES_FN)
    op.execute(_ARTIFACTS_FN)
    op.execute(
        "CREATE TRIGGER messages_branch_totals AFTER INSERT OR UPDATE OR DELETE "
        "ON messages FOR EACH ROW EXECUTE FUNCTION trg_messages_branch_totals();"
    )
    op.execute(
        "CREATE TRIGGER artifacts_user_bytes AFTER INSERT OR UPDATE OR DELETE "
        "ON artifacts FOR EACH ROW EXECUTE FUNCTION trg_artifacts_user_bytes();"
    )
    op.execute(
        """
        UPDATE conversations c SET
            bt_total_cost_micro_rub   = agg.cost,
            bt_total_input_tokens     = agg.in_tok,
            bt_total_output_tokens    = agg.out_tok,
            bt_total_reasoning_tokens = agg.reason_tok,
            bt_message_count          = agg.msg_count,
            bt_generation_count       = agg.gen_count
        FROM (
            SELECT conversation_id,
                COALESCE(SUM(cost_micro_rub), 0)     AS cost,
                COALESCE(SUM(input_tokens), 0)       AS in_tok,
                COALESCE(SUM(output_tokens), 0)      AS out_tok,
                COALESCE(SUM(reasoning_tokens), 0)   AS reason_tok,
                COUNT(*)                             AS msg_count,
                COUNT(*) FILTER (WHERE role = 'assistant') AS gen_count
            FROM messages
            WHERE deleted_at IS NULL
            GROUP BY conversation_id
        ) agg
        WHERE c.id = agg.conversation_id;
        """
    )
    op.execute(
        "UPDATE users u SET artifact_bytes_used = "
        "COALESCE((SELECT SUM(size_bytes) FROM artifacts a WHERE a.user_id = u.id), 0);"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS messages_branch_totals ON messages;")
    op.execute("DROP TRIGGER IF EXISTS artifacts_user_bytes ON artifacts;")
    op.execute("DROP FUNCTION IF EXISTS trg_messages_branch_totals();")
    op.execute("DROP FUNCTION IF EXISTS trg_artifacts_user_bytes();")
    op.drop_index("idx_artifacts_conv_filename", table_name="artifacts")
    op.drop_column("users", "artifact_bytes_used")
    for col in _BT_COLS:
        op.drop_column("conversations", col)
