# Operations: cron jobs, backups, restore

## Cron jobs & backup

Trinity runs recurring jobs on the host (SPEC.md §12). Most run inside the backend
container via `docker compose exec -T`; `backup.sh` runs directly on the host
because it needs `pg_dump`, `gzip`, `aws`, and `curl`.

**Note:** the financial reconciliation and the reservation sweep are NOT host-cron
jobs — they run in-process under the Redis leader lock (`app/main.py`), so do not
add them to a crontab (a host cron + the in-process loop is the double-runner race,
tr-6qf7 / tr-376l). See SPEC.md §12 table A.

| Schedule (UTC) | Job | Command |
|---|---|---|
| Every 5 min | OpenRouter health probe | `python -m app.tasks.health_check` |
| Daily 03:00 | Supplemental pg_dump → S3 | `bash scripts/backup.sh` |
| Daily 04:00 | Data retention purge | `python -m app.tasks.cleanup retention` |

### Install / refresh

```bash
bash scripts/setup_cron.sh
```

The installer is **idempotent**: it locates a `# >>> trinity cron block <<<`
marker in the user's crontab and replaces it atomically, so re-running never
creates duplicates. Logs accumulate under `logs/cron/`.

To uninstall:

```bash
bash scripts/setup_cron.sh --uninstall
```

### Trigger jobs manually

```bash
# Reservation sweep (used in 1-min cron):
docker compose exec backend python -m app.tasks.cleanup reservations

# Data retention purge (4 AM cron):
docker compose exec backend python -m app.tasks.cleanup retention

# Both at once:
docker compose exec backend python -m app.tasks.cleanup all

# OpenRouter health probe:
docker compose exec backend python -m app.tasks.health_check

# Financial reconciliation — ONE-OFF manual check only (the in-process leader loop
# already runs it daily; do NOT schedule this in cron — double-runner race):
docker compose exec backend python -m app.tasks.reconciliation

# Backup (run on the host, not in the container):
bash scripts/backup.sh
```

### Backups

- **Storage:** Yandex Object Storage, bucket `your-backup-bucket`
  (endpoint `https://storage.yandexcloud.net`).
- **Format:** `trinity_backup_YYYYMMDD_HHMMSS.sql.gz` produced by
  `pg_dump --serializable-deferrable --format=plain --no-owner --no-privileges | gzip`.
- **Retention:** the 30 most recent backups are kept; older ones are pruned
  automatically each run.
- **Notifications:** success and failure are reported to the admin Telegram
  channel via `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` from `.env`.

### Restore from backup

```bash
# List available backups:
aws s3 ls "s3://${S3_BUCKET}/" --endpoint-url "${S3_ENDPOINT}"

# Stop the backend first so in-flight writes don't corrupt the restore:
docker compose stop backend

# Restore (the script prints a destructive-action warning and requires
# typing YES to proceed):
bash scripts/restore.sh trinity_backup_YYYYMMDD_HHMMSS.sql.gz

# Restart the backend:
docker compose start backend
```

The restore script downloads the named archive from S3, decompresses it,
and pipes it into `psql "$DATABASE_URL"`. The asyncpg DSN used by the app
is auto-converted to a libpq URL.
