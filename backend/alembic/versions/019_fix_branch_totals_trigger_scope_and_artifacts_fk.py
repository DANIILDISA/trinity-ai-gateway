"""make active_leaf FK deferrable (fixes the branch-totals trigger vs bulk-delete
conflict) + normalize the artifacts->conversations FK to CASCADE

Follow-up to migration 018 (tr-9wq), fixing two issues found in testing:

1. The `messages_branch_totals` AFTER ROW trigger (018) UPDATEs the parent
   conversation row on every message change, INCLUDING DELETE. On a bulk
   `DELETE FROM messages` (conftest cleanup; prod permanent-delete) that also
   removes a message referenced by `conversations.active_leaf_id`, the trigger's
   per-row conversation UPDATE conflicts with the `fk_conversations_active_leaf`
   (active_leaf_id -> messages.id) ON DELETE SET NULL referential action and raises
   a spurious ForeignKeyViolationError mid-statement. The canonical fix for a
   row-trigger modifying a table involved in the same statement's FK cascade is to
   make that FK DEFERRABLE INITIALLY DEFERRED, so the check runs at COMMIT — after
   every SET NULL and trigger UPDATE has settled. The trigger keeps full
   INSERT/UPDATE/DELETE handling (so a hard message-delete still decrements bt_*).
   Verified: with the deferrable FK the bulk-delete cleanup AND the hard-delete
   trigger test both pass. (tr-9wq follow-up.)

2. `artifacts_conversation_id_fkey` drifted to ON DELETE SET NULL on trinity_test
   (migration 017 + the ORM model + dev trinity all declare CASCADE — likely 017 was
   edited after trinity_test was first stamped). Recreate it CASCADE so a conversation
   permanent-delete cascade-deletes artifacts (firing artifacts_user_bytes to release
   the per-user quota) instead of orphaning them. No-op where already CASCADE. (tr-30k.)

Revision ID: 019
Revises: 018
Create Date: 2026-06-17
"""
from __future__ import annotations

from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # (1) active_leaf FK → DEFERRABLE INITIALLY DEFERRED (keep ON DELETE SET NULL).
    op.execute("ALTER TABLE conversations DROP CONSTRAINT IF EXISTS fk_conversations_active_leaf;")
    op.execute(
        "ALTER TABLE conversations ADD CONSTRAINT fk_conversations_active_leaf "
        "FOREIGN KEY (active_leaf_id) REFERENCES messages(id) "
        "ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED;"
    )
    # (2) artifacts→conversations FK → CASCADE (declared by 017 / the ORM model).
    op.execute("ALTER TABLE artifacts DROP CONSTRAINT IF EXISTS artifacts_conversation_id_fkey;")
    op.execute(
        "ALTER TABLE artifacts ADD CONSTRAINT artifacts_conversation_id_fkey "
        "FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE;"
    )


def downgrade() -> None:
    # Restore the active_leaf FK to a non-deferrable SET NULL. The artifacts FK
    # normalization is a forward-only drift correction and is not reverted.
    op.execute("ALTER TABLE conversations DROP CONSTRAINT IF EXISTS fk_conversations_active_leaf;")
    op.execute(
        "ALTER TABLE conversations ADD CONSTRAINT fk_conversations_active_leaf "
        "FOREIGN KEY (active_leaf_id) REFERENCES messages(id) ON DELETE SET NULL;"
    )
