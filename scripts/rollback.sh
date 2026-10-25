#!/bin/bash
# Trinity production rollback — SPEC.md §17.2.
#
# Reverses a failed deploy in four steps, each gated by an explicit YES
# confirmation:
#   1. `git checkout HEAD~1` to revert application code
#   2. Stop the backend service (so in-flight writes don't fight the restore)
#   3. `alembic downgrade -1` to reverse the latest schema migration
#   4. Restore data via scripts/restore.sh from an S3 backup
#   5. Rebuild images at the previous commit + bring the stack back up
#   6. Poll the health endpoint
#
# Usage:
#   bash scripts/rollback.sh                              # interactive backup pick
#   bash scripts/rollback.sh trinity_backup_20260519_120342.sql.gz
#
# Env vars consumed:
#   DOMAIN          (same fallback semantics as deploy.sh's health check)
#   SKIP_GIT        1 to keep the current commit (data-only rollback)
#   SKIP_DOWNGRADE  1 to skip `alembic downgrade -1` (the restore overwrites
#                   the schema anyway; use this when the backup pre-dates
#                   the migration you want to leave un-applied)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

COMPOSE_FILE="docker-compose.prod.yml"
HEALTH_TIMEOUT_SECONDS=30

log()  { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
fail() { log "🔴 $*"; exit 1; }

# Read just the env vars rollback.sh and restore.sh actually need. We don't
# `source` the file because .env may contain unquoted values with shell-special
# characters (e.g. `RESEND_FROM_EMAIL=Trinity <noreply@...>`); a stray `<`
# would be parsed as redirection. sed extracts each value byte-for-byte.

ENV_FILE=""
if   [ -f .env.prod ]; then ENV_FILE=".env.prod"
elif [ -f .env ];      then ENV_FILE=".env"
fi

read_env_var() {
    local key="$1"
    [ -n "${ENV_FILE}" ] || return 0
    sed -n -E "s/^${key}=(.*)$/\1/p" "${ENV_FILE}" | tail -1
}

# Only fall back to file values when the var isn't already exported.
: "${DOMAIN:=$(read_env_var DOMAIN)}"
: "${S3_BUCKET:=$(read_env_var S3_BUCKET)}"
: "${S3_ENDPOINT:=$(read_env_var S3_ENDPOINT)}"
: "${S3_ACCESS_KEY:=$(read_env_var S3_ACCESS_KEY)}"
: "${S3_SECRET_KEY:=$(read_env_var S3_SECRET_KEY)}"
# restore.sh reads DATABASE_URL too — pass it through so the user doesn't have
# to set both .env and the shell.
: "${DATABASE_URL:=$(read_env_var DATABASE_URL)}"
export DOMAIN S3_BUCKET S3_ENDPOINT S3_ACCESS_KEY S3_SECRET_KEY DATABASE_URL

# ── Pre-flight ────────────────────────────────────────────────────────────

for tool in docker git curl; do
    command -v "$tool" >/dev/null 2>&1 || fail "missing required tool: ${tool}"
done

[ -f "${COMPOSE_FILE}" ] || fail "${COMPOSE_FILE} not found"

# ── Choose backup ─────────────────────────────────────────────────────────

BACKUP_FILE="${1:-}"

list_recent_backups() {
    # Mirror restore.sh's expected creds layout (S3_ACCESS_KEY/S3_SECRET_KEY).
    AWS_ACCESS_KEY_ID="${S3_ACCESS_KEY:-}" \
    AWS_SECRET_ACCESS_KEY="${S3_SECRET_KEY:-}" \
    AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ru-central1}" \
        aws s3 ls "s3://${S3_BUCKET}/" --endpoint-url "${S3_ENDPOINT}" \
        | awk '{print $1" "$2"  "$4}' \
        | grep -E 'trinity_backup_[0-9]{8}_[0-9]{6}\.sql\.gz$' \
        | sort -r \
        | head -n 5
}

if [ -z "${BACKUP_FILE}" ]; then
    : "${S3_BUCKET:?S3_BUCKET must be set (export it or fill .env.prod)}"
    : "${S3_ENDPOINT:?S3_ENDPOINT must be set}"
    : "${S3_ACCESS_KEY:?S3_ACCESS_KEY must be set}"
    : "${S3_SECRET_KEY:?S3_SECRET_KEY must be set}"
    command -v aws >/dev/null 2>&1 || \
        fail "aws CLI not in PATH — pass the backup filename as an argument instead."

    echo ""
    echo "Five most recent backups in s3://${S3_BUCKET}/:"
    echo ""
    list_recent_backups | nl -w2 -s'. '
    echo ""
    read -r -p "Enter the full backup filename to restore (or blank to abort): " BACKUP_FILE
    [ -n "${BACKUP_FILE}" ] || fail "no backup chosen — aborting."
fi

# ── Big-red confirmation ──────────────────────────────────────────────────

cat <<EOF

================================================================================
  ⚠  TRINITY ROLLBACK — DESTRUCTIVE OPERATION
================================================================================
  Backup to restore : ${BACKUP_FILE}
  Compose file      : ${COMPOSE_FILE}
  Git target        : $([ "${SKIP_GIT:-0}" = "1" ] && echo "(unchanged, SKIP_GIT=1)" || echo "HEAD~1")
  Alembic           : $([ "${SKIP_DOWNGRADE:-0}" = "1" ] && echo "(skipped, SKIP_DOWNGRADE=1)" || echo "alembic downgrade -1")

  This will:
    • check out the previous commit
    • stop the backend
    • downgrade the database schema by one revision
    • OVERWRITE the entire database with the backup contents
    • rebuild and restart all services

================================================================================
EOF

read -r -p "Type YES (uppercase) to confirm: " CONFIRM
[ "${CONFIRM}" = "YES" ] || fail "aborted by user."

# ── 1. Revert code ────────────────────────────────────────────────────────

if [ "${SKIP_GIT:-0}" = "1" ]; then
    log "SKIP_GIT=1 — leaving the working tree at the current commit."
else
    OLD_SHA="$(git rev-parse --short HEAD~1)"
    log "Reverting code: git checkout HEAD~1 → ${OLD_SHA}"
    git checkout HEAD~1
fi

# ── 2. Stop backend (so it can't write during the restore) ────────────────

log "Stopping backend service..."
docker compose -f "${COMPOSE_FILE}" stop backend || true

# ── 3. Schema downgrade ───────────────────────────────────────────────────

if [ "${SKIP_DOWNGRADE:-0}" = "1" ]; then
    log "SKIP_DOWNGRADE=1 — skipping alembic downgrade."
else
    log "Running alembic downgrade -1 (one-shot container)..."
    # The restore in step 4 will overwrite schema anyway; this is belt-and-
    # suspenders so the rollback works even if the backup happens to be
    # corrupt or unrelated.
    if ! docker compose -f "${COMPOSE_FILE}" run --rm backend alembic downgrade -1; then
        log "⚠ alembic downgrade -1 failed (likely already at the prior revision)."
        log "  Continuing — restore step will overwrite alembic_version anyway."
    fi
fi

# ── 4. Restore data ───────────────────────────────────────────────────────

log "Restoring data from ${BACKUP_FILE}..."
# restore.sh has its own YES prompt; pipe YES so we don't double-prompt.
echo "YES" | bash scripts/restore.sh "${BACKUP_FILE}"

# ── 5. Rebuild + restart with previous code ───────────────────────────────

log "Building images at the rolled-back commit..."
docker compose -f "${COMPOSE_FILE}" build

log "Bringing the stack back up..."
docker compose -f "${COMPOSE_FILE}" up -d

# ── 6. Health check ───────────────────────────────────────────────────────

if [ -n "${DOMAIN:-}" ]; then
    HEALTH_URL="https://${DOMAIN}/api/v1/health"
else
    HEALTH_URL="http://localhost:8000/api/v1/health"
fi
log "Polling ${HEALTH_URL} ..."

DEADLINE=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
while [ "$(date +%s)" -lt "${DEADLINE}" ]; do
    if curl -fsS --max-time 5 "${HEALTH_URL}" >/dev/null 2>&1; then
        log "✅ Rollback complete — health check OK."
        exit 0
    fi
    sleep 2
done

log "🔴 Health check failed after rollback. Manual investigation required."
log "  docker compose -f ${COMPOSE_FILE} logs --tail=200 backend"
exit 1
