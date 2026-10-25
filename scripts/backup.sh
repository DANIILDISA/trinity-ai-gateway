#!/bin/bash
# Supplemental pg_dump → S3-compatible storage. Runs daily at 03:00 UTC via cron.
# SPEC.md §12.2.
#
# Required env vars (from .env or already exported):
#   DATABASE_URL, S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET,
#   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
#
# Required CLI tools on the host: pg_dump, gzip, aws (awscli), curl.
# The script is intended to be invoked by HOST cron — not from inside the
# backend container — so the host environment must provide these binaries.

set -euo pipefail

TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
BACKUP_FILE="trinity_backup_${TIMESTAMP}.sql.gz"
LOCAL_PATH="/tmp/${BACKUP_FILE}"

# ── Helpers ────────────────────────────────────────────────────────────────

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

notify_telegram() {
    # Send a Telegram message; never fail the script if Telegram is down.
    local text="$1"
    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
        log "WARN: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set; skipping notification."
        return 0
    fi
    curl -sS --max-time 10 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_CHAT_ID}" \
        -d text="${text}" >/dev/null || log "WARN: Telegram send failed."
}

on_failure() {
    local exit_code=$?
    local where="${BASH_COMMAND}"
    log "ERROR: backup failed at: ${where} (exit ${exit_code})"
    notify_telegram "🔴 CRITICAL: Trinity backup FAILED at ${TIMESTAMP} (${where}, exit ${exit_code})"
    rm -f "${LOCAL_PATH}" 2>/dev/null || true
    exit "${exit_code}"
}
trap on_failure ERR

# ── Pre-flight ────────────────────────────────────────────────────────────

for tool in pg_dump gzip aws curl; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        log "ERROR: required tool '$tool' not found in PATH."
        notify_telegram "🔴 CRITICAL: Trinity backup ABORTED — '$tool' not installed on host."
        exit 127
    fi
done

: "${DATABASE_URL:?DATABASE_URL must be set}"
: "${S3_ENDPOINT:?S3_ENDPOINT must be set}"
: "${S3_BUCKET:?S3_BUCKET must be set}"
: "${S3_ACCESS_KEY:?S3_ACCESS_KEY must be set}"
: "${S3_SECRET_KEY:?S3_SECRET_KEY must be set}"

# awscli reads credentials from these standard variables.
export AWS_ACCESS_KEY_ID="${S3_ACCESS_KEY}"
export AWS_SECRET_ACCESS_KEY="${S3_SECRET_KEY}"
# Region is irrelevant for Yandex/S3-compatible but awscli refuses to run
# without one. Default to ru-central1 (Yandex) — overridable from caller.
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ru-central1}"

# Translate the asyncpg DSN used by the backend to a vanilla libpq URL so
# pg_dump understands it. asyncpg → postgresql, no other changes.
PG_DUMP_URL="${DATABASE_URL/postgresql+asyncpg:\/\//postgresql:\/\/}"

# ── Dump ───────────────────────────────────────────────────────────────────

log "Starting backup: ${BACKUP_FILE}"
pg_dump "${PG_DUMP_URL}" \
    --serializable-deferrable \
    --format=plain \
    --no-owner \
    --no-privileges \
    | gzip > "${LOCAL_PATH}"

BACKUP_SIZE=$(stat -c%s "${LOCAL_PATH}" 2>/dev/null || stat -f%z "${LOCAL_PATH}")
log "Dump complete: ${LOCAL_PATH} (${BACKUP_SIZE} bytes)"

# ── Upload ────────────────────────────────────────────────────────────────

aws s3 cp "${LOCAL_PATH}" "s3://${S3_BUCKET}/${BACKUP_FILE}" \
    --endpoint-url "${S3_ENDPOINT}"

log "Uploaded to s3://${S3_BUCKET}/${BACKUP_FILE}"
rm -f "${LOCAL_PATH}"

# ── Prune (keep last 30) ──────────────────────────────────────────────────

log "Pruning backups older than 30 days (keep last 30 by name)..."
aws s3 ls "s3://${S3_BUCKET}/" --endpoint-url "${S3_ENDPOINT}" \
    | awk '{print $4}' \
    | grep -E '^trinity_backup_[0-9]{8}_[0-9]{6}\.sql\.gz$' \
    | sort \
    | head -n -30 \
    | while read -r old; do
        log "Deleting old backup: ${old}"
        aws s3 rm "s3://${S3_BUCKET}/${old}" --endpoint-url "${S3_ENDPOINT}"
    done || true

# ── Notify success ────────────────────────────────────────────────────────

notify_telegram "🟢 Trinity backup OK: ${BACKUP_FILE} (${BACKUP_SIZE} bytes)"
log "Backup complete."
