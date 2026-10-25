#!/bin/bash
# Restore the Trinity database from an S3 backup. SPEC.md §12.3.
#
# Usage:
#   bash scripts/restore.sh trinity_backup_20250101_030000.sql.gz
#
# Required env vars (from .env or already exported):
#   DATABASE_URL, S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET
#
# Required CLI tools on the host: aws (awscli), gunzip, psql.

set -euo pipefail

# ── Args ───────────────────────────────────────────────────────────────────

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <backup-filename>"
    echo "Example: $0 trinity_backup_20250101_030000.sql.gz"
    exit 2
fi

BACKUP_FILE="$1"
LOCAL_GZ="/tmp/restore_${BACKUP_FILE}"
LOCAL_SQL="${LOCAL_GZ%.gz}"

# ── Pre-flight ────────────────────────────────────────────────────────────

for tool in aws gunzip psql; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERROR: required tool '$tool' not found in PATH."
        exit 127
    fi
done

: "${DATABASE_URL:?DATABASE_URL must be set}"
: "${S3_ENDPOINT:?S3_ENDPOINT must be set}"
: "${S3_BUCKET:?S3_BUCKET must be set}"
: "${S3_ACCESS_KEY:?S3_ACCESS_KEY must be set}"
: "${S3_SECRET_KEY:?S3_SECRET_KEY must be set}"

export AWS_ACCESS_KEY_ID="${S3_ACCESS_KEY}"
export AWS_SECRET_ACCESS_KEY="${S3_SECRET_KEY}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ru-central1}"

PG_URL="${DATABASE_URL/postgresql+asyncpg:\/\//postgresql:\/\/}"

# ── Warning + confirmation ────────────────────────────────────────────────

cat <<EOF

================================================================================
  ⚠  TRINITY RESTORE — DESTRUCTIVE OPERATION
================================================================================

  You are about to restore the database from:
      s3://${S3_BUCKET}/${BACKUP_FILE}

  This will OVERWRITE all current data in:
      ${PG_URL}

  All existing users, transactions, conversations, messages, alerts, and
  notifications WILL BE REPLACED by the contents of the backup.

  Stop the backend service first (so no in-flight writes corrupt the restore):
      docker compose stop backend

  After the restore completes, restart it:
      docker compose start backend

================================================================================

EOF

read -r -p "Type YES (in uppercase) to confirm: " CONFIRM
if [ "${CONFIRM}" != "YES" ]; then
    echo "Aborted."
    exit 1
fi

# ── Download ──────────────────────────────────────────────────────────────

echo "[*] Downloading s3://${S3_BUCKET}/${BACKUP_FILE} ..."
aws s3 cp "s3://${S3_BUCKET}/${BACKUP_FILE}" "${LOCAL_GZ}" \
    --endpoint-url "${S3_ENDPOINT}"

# ── Decompress ────────────────────────────────────────────────────────────

echo "[*] Decompressing ..."
# -f to overwrite any stale leftover
gunzip -f "${LOCAL_GZ}"

# ── Restore ───────────────────────────────────────────────────────────────

echo "[*] Restoring into ${PG_URL} ..."
psql "${PG_URL}" < "${LOCAL_SQL}"

# ── Cleanup ───────────────────────────────────────────────────────────────

rm -f "${LOCAL_SQL}"
echo "[+] Restore complete."
