#!/bin/bash
# Trinity production deployment — SPEC.md §17.1.
#
# Drives a manual blue-ish deploy: snapshot the DB, pull latest code,
# guard against a pass-only downgrade in the new migration, rebuild
# images, run `alembic upgrade head`, restart the stack, and poll the
# health endpoint. On health-check failure the script prints the exact
# rollback invocation and exits non-zero — it does NOT auto-rollback.
#
# Env vars consumed:
#   DEPLOY_BRANCH   default: main
#   DOMAIN          when set, health check hits https://${DOMAIN}/api/v1/health;
#                   otherwise falls back to http://localhost:8000/api/v1/health
#   SKIP_BACKUP     1 to skip the pre-deploy backup (e.g. dry-runs)
#
# Run from the project root:
#   bash scripts/deploy.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

COMPOSE_FILE="docker-compose.prod.yml"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
HEALTH_TIMEOUT_SECONDS=30

log()  { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
fail() { log "🔴 $*"; exit 1; }

# ── 1. Pre-flight ──────────────────────────────────────────────────────────

log "=== Trinity Deploy (branch: ${DEPLOY_BRANCH}) ==="

for tool in docker git curl; do
    command -v "$tool" >/dev/null 2>&1 || fail "missing required tool: ${tool}"
done

[ -f .env.prod ] || fail ".env.prod is missing — copy .env.prod.example and fill in values"
[ -f "${COMPOSE_FILE}" ] || fail "${COMPOSE_FILE} not found"

# ── 2. Backup (snapshot before any change) ────────────────────────────────

if [ "${SKIP_BACKUP:-0}" = "1" ]; then
    log "SKIP_BACKUP=1 — skipping pre-deploy backup."
else
    log "Running pre-deploy backup..."
    if ! bash scripts/backup.sh; then
        fail "pre-deploy backup failed — aborting before touching any code."
    fi
    log "Pre-deploy backup completed."
fi

# ── 3. Pull latest code ────────────────────────────────────────────────────

log "Fetching origin and resetting to ${DEPLOY_BRANCH}..."
git fetch origin "${DEPLOY_BRANCH}"
git checkout "${DEPLOY_BRANCH}"
git pull --ff-only origin "${DEPLOY_BRANCH}"

NEW_SHA="$(git rev-parse --short HEAD)"
log "Now at ${NEW_SHA}"

# ── 4. Migration safety check ─────────────────────────────────────────────

log "Verifying latest migration has a real downgrade() body..."
if ! bash scripts/check_migration.sh; then
    echo ""
    read -r -p "Latest migration has no downgrade body. Proceed anyway? Type YES: " CONFIRM
    [ "${CONFIRM}" = "YES" ] || fail "aborted — fix the migration first."
fi

# ── 5. Build new images ────────────────────────────────────────────────────

log "Building production images (no cache)..."
docker compose -f "${COMPOSE_FILE}" build --no-cache

# ── 6. Run database migrations ─────────────────────────────────────────────
# `run --rm` spins up a one-shot backend container that exits after alembic
# finishes — avoids racing the live backend service which might still be
# holding pooled connections.

log "Applying database migrations..."
docker compose -f "${COMPOSE_FILE}" run --rm backend alembic upgrade head

# ── 7. Restart services ────────────────────────────────────────────────────
# SPEC.md §17.1 restarts backend first so the frontend's new bundle never
# requests a route the backend hasn't shipped yet.

log "Restarting backend..."
docker compose -f "${COMPOSE_FILE}" up -d --no-deps backend
sleep 5

log "Restarting frontend..."
docker compose -f "${COMPOSE_FILE}" up -d --no-deps frontend

log "Ensuring all services up..."
docker compose -f "${COMPOSE_FILE}" up -d

# ── 8. Health check (poll up to HEALTH_TIMEOUT_SECONDS) ────────────────────

if [ -n "${DOMAIN:-}" ]; then
    HEALTH_URL="https://${DOMAIN}/api/v1/health"
else
    HEALTH_URL="http://localhost:8000/api/v1/health"
fi
log "Polling ${HEALTH_URL} ..."

DEADLINE=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
while [ "$(date +%s)" -lt "${DEADLINE}" ]; do
    if curl -fsS --max-time 5 "${HEALTH_URL}" >/dev/null 2>&1; then
        log "✅ Health OK"
        log "=== Deploy complete: ${NEW_SHA} ==="
        exit 0
    fi
    sleep 2
done

# Health check failed — surface the exact rollback command.
log "🔴 Health check failed after ${HEALTH_TIMEOUT_SECONDS}s."
cat <<EOF

Rollback steps:
  1. Check logs:
       docker compose -f ${COMPOSE_FILE} logs --tail=200 backend frontend
  2. Roll back code + DB:
       bash scripts/rollback.sh             # interactive: pick backup
       bash scripts/rollback.sh trinity_backup_YYYYMMDD_HHMMSS.sql.gz

EOF
exit 1
