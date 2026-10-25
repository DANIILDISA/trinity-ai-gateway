#!/bin/bash
# Cron-based health check → Telegram alert on failure
# Crontab entry: */5 * * * * /opt/trinity/scripts/health_cron.sh
set -euo pipefail

HEALTH_URL="http://localhost:8000/api/v1/health"

if ! curl -sf "$HEALTH_URL" > /dev/null; then
  curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d chat_id="${TELEGRAM_CHAT_ID}" \
    -d text="🔴 CRITICAL: Trinity health check failed at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi
