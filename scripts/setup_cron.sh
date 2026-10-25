#!/bin/bash
# Install Trinity host cron jobs. SPEC.md §13.6 + Task 9.6.
#
# Idempotent: re-running replaces any previous Trinity block in the user's
# crontab so duplicate entries are impossible. Run as the user whose crontab
# you want to own these jobs (the user that can also run `docker compose`).
#
# Usage:
#   bash scripts/setup_cron.sh           # install/refresh
#   bash scripts/setup_cron.sh --uninstall  # remove
#
# After install, view the active crontab with:
#   crontab -l

set -euo pipefail

# Locate the project root: parent of the scripts/ directory containing us.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Markers used to find and replace the Trinity block atomically.
MARK_BEGIN="# >>> trinity cron block (managed by setup_cron.sh) >>>"
MARK_END="# <<< trinity cron block <<<"

# Docker compose is invoked with -T so it doesn't require a TTY (cron has none).
# We `cd` into the project root first so docker compose finds docker-compose.yml.
EXEC="cd ${PROJECT_ROOT} && docker compose exec -T backend python -m"
BACKUP_CMD="cd ${PROJECT_ROOT} && bash ${PROJECT_ROOT}/scripts/backup.sh"
LOG_DIR="${PROJECT_ROOT}/logs/cron"

mkdir -p "${LOG_DIR}"

# Build the new block.
read -r -d '' NEW_BLOCK <<EOF || true
${MARK_BEGIN}
# Trinity cron jobs — SPEC.md §13.6. Times are HOST local; if you want strict
# UTC schedules, run \`cron\` itself in UTC or add a TZ= line above this block.
#
# NOTE: the every-minute reservation reaper (cleanup reservations) and the
# partial-cost reconcile (cleanup reconcile) used to run here. They now run
# IN-PROCESS under the Redis leader lock in app/main.py (_reservation_cleanup_loop)
# so they fire even where this host crontab is never installed (the Docker/Windows
# deployment had no cron at all — tr-u3y5). Do NOT re-add them here: a host cron
# AND the in-process loop both sweeping every minute is the double-runner race
# (tr-6qf7). The reaper is now guarded against it, but pick exactly one owner
# anyway to avoid wasted DB load and confusing duplicate logs — and that owner is
# the in-process loop.
#
# The daily financial reconciliation (BILLING_LOGIC.md §12) is likewise now
# in-process (_daily_reconciliation_loop, leader-only, tr-376l) — do NOT re-add its
# host-cron line here; a host cron plus the in-process loop is the same
# double-runner race (duplicate ledger alerts / Telegram spam).
#
# Every 5 min  — OpenRouter health probe (balance + per-model availability)
*/5 * * * * ${EXEC} app.tasks.health_check >> ${LOG_DIR}/health_check.log 2>&1
#
# Every 1 min  — CBR exchange-rate auto-update. The task self-throttles to
# auto_exchange_rate_interval_minutes and no-ops when disabled, so a 1-min
# cron is safe and keeps the effective interval accurate.
* * * * * ${EXEC} app.tasks.exchange_rate >> ${LOG_DIR}/exchange_rate.log 2>&1
#
# Daily 03:00 UTC — supplemental pg_dump → S3
0 3 * * * ${BACKUP_CMD} >> ${LOG_DIR}/backup.log 2>&1
#
# Daily 04:00 UTC — data retention purge (SCHEMA.md §6)
0 4 * * * ${EXEC} app.tasks.cleanup retention >> ${LOG_DIR}/cleanup_retention.log 2>&1
${MARK_END}
EOF

# ── Read current crontab (empty is fine) ───────────────────────────────────

CURRENT="$(crontab -l 2>/dev/null || true)"

# ── Strip any existing Trinity block (between the markers) ────────────────

STRIPPED="$(printf '%s' "${CURRENT}" | awk -v b="${MARK_BEGIN}" -v e="${MARK_END}" '
    $0 == b {inside=1; next}
    $0 == e {inside=0; next}
    !inside {print}
')"

# ── Handle --uninstall ────────────────────────────────────────────────────

if [ "${1:-}" = "--uninstall" ]; then
    if [ -z "${STRIPPED}" ]; then
        echo "[*] Installing empty crontab (no other entries existed)."
        crontab -r 2>/dev/null || true
    else
        printf '%s\n' "${STRIPPED}" | crontab -
    fi
    echo "[+] Trinity cron block removed."
    exit 0
fi

# ── Install / refresh ─────────────────────────────────────────────────────

# Append a trailing newline if the stripped content didn't end with one, then
# add the new block.
{
    if [ -n "${STRIPPED}" ]; then
        printf '%s\n' "${STRIPPED}"
    fi
    printf '%s\n' "${NEW_BLOCK}"
} | crontab -

echo "[+] Trinity cron block installed/refreshed."
echo ""
echo "Active crontab:"
crontab -l | sed 's/^/  /'
echo ""
echo "Cron logs will accumulate under: ${LOG_DIR}"
echo "To uninstall: bash scripts/setup_cron.sh --uninstall"
