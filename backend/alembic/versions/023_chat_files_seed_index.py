"""chat_files partial covering index for the B3 seed-hash hot-path query (tr-f63e)."""
from alembic import op

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_files_conv_included_created "
        "ON chat_files (conversation_id, created_at) "
        "INCLUDE (id, filename) WHERE included_in_context IS TRUE"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chat_files_conv_included_created")
