# Foundation Integration Test Results

**Date:** 2026-05-13
**Scope:** Tasks 0.1–0.6 (project scaffold, FastAPI app, Docker stack, DB/Redis layer, Alembic migration, currency utilities)
**Verdict:** **PASS** — all 9 sections green. Foundation is safe to build Phase 1 (Auth) on top of.

---

## 1. Static Structure Verification — PASS

| Item | Status |
|---|---|
| `backend/` with `app/`, `alembic/`, `tests/` | ✓ |
| `backend/alembic.ini`, `backend/alembic/env.py`, `backend/alembic/versions/001_initial_schema.py` | ✓ |
| `backend/Dockerfile`, `backend/pyproject.toml` | ✓ |
| 14 ORM model files in `app/models/` + `enums.py` + `__init__.py` | ✓ |
| `app/core/`: `constants.py`, `database.py`, `redis.py`, `exceptions.py`, `middleware.py`, `security.py` | ✓ |
| Root: `.env` (28 vars), `docker-compose.yml`, `docker-compose.prod.yml`, `CLAUDE.md`, `SPEC.md`, `SCHEMA.md`, `BILLING_LOGIC.md`, `ROADMAP.md` | ✓ |

## 2. FastAPI Application Import — PASS

`python -c "from app.main import app"` succeeds inside container.

| Probe | Value |
|---|---|
| `app.title` / `app.version` | `Trinity` / `0.0.1` |
| Registered routes | 5 |
| `Base.metadata.tables` size | **14** |
| `settings.database_url` | `postgresql+asyncpg://trinity:trinity@postgres:5432/...` |
| `settings.redis_url` | `redis://redis:6379/0` |
| Exception handlers | `TrinityError`, `RequestValidationError`, `Exception` (+ Starlette defaults) |

## 3. Docker Compose Integration — PASS

```
NAME                        STATUS          PORTS
projecttrinity-backend-1    Up              0.0.0.0:8000->8000/tcp
projecttrinity-postgres-1   Up              0.0.0.0:5432->5432/tcp
projecttrinity-redis-1      Up              0.0.0.0:6379->6379/tcp
```

`docker compose logs --since 5m` — no real WARNING/ERROR lines. The only `WARNING:` entries are uvicorn `--reload` file-change notifications (expected in dev mode).

## 4. Database Layer via HTTP — PASS

```
$ curl http://localhost:8000/api/v1/health
{"status":"healthy","checks":{"database":"ok","redis":"ok"},"version":"0.0.1"}
```

DB and Redis both reachable through the FastAPI lifespan-managed pools.

## 5. Alembic Migration Verification — PASS

| Check | Expected | Actual |
|---|---|---|
| `alembic current` | `001 (head)` | `001 (head)` |
| Schema tables (excl. `alembic_version`) | 14 | **14** |
| Indexes named `idx_*` | 27 (SCHEMA.md §4) | **27** |
| `system_settings` rows | 13 (SCHEMA.md §7.4) | **13** |
| PostgreSQL ENUM types | 6 | **6** |

Tables: `users, refresh_tokens, password_reset_tokens, transactions, reservations, conversations, messages, usage_logs, model_pricing, api_keys, system_settings, admin_audit_log, alerts, notifications`.

Enums: `transaction_type, reservation_status, message_role, alert_severity, alert_status, notification_channel`.

## 6. Database Constraints Integration — PASS

Constraints enforced at the **DB layer** (not just app):

| Constraint | Test | Result |
|---|---|---|
| `chk_users_balance_nonneg` / `chk_available_balance` | `INSERT users (balance_micro_rub=-1)` | Rejected (`chk_available_balance`) |
| `chk_available_balance` | `INSERT users (balance=100, reserved=200)` | Rejected (`chk_available_balance`) |
| `uq_transactions_idempotency` | Two INSERTs with same `idempotency_key` | 2nd rejected (UNIQUE violation) |
| FK NO ACTION on `transactions.user_id` | `DELETE FROM users` with active transaction | Rejected (FK violation) |
| FK CASCADE on `refresh_tokens.user_id` | `DELETE FROM users` with active token | Cascaded; token removed |
| ENUM `transaction_type` | `UPDATE transactions SET type='bogus_type'` | Rejected (invalid enum value) |

## 7. Currency Functions Integration — PASS

| Check | Result |
|---|---|
| `pytest tests/test_constants.py` | **41 passed** in 0.19s |
| Round-trip 500 RUB → micro → display | `500_000_000` → `"500.00"` ✓ |
| YooKassa flow: 12_345 kopeks → display | `123_450_000` → `"123.45"` ✓ |
| OpenRouter $0.0042 → μUSD → μRUB @ 82.5 | `4200` → `346_500` (ceiling) ✓ |
| Token cost 1500 @ 320 μ₽/M → API string | `480_000` → `"0.4800"` ✓ |

All 7 functions exist with int return types — no float leaks past `usd_float_to_micro_usd` (verified by `test_no_float_in_return_types`).

## 8. DI / Connection Leak Check — PASS

Baseline backend-held connections in `pg_stat_activity` for `datname='trinity'`:

| Stage | Count |
|---|---|
| Baseline (idle) | 1 |
| After 10 sequential `GET /health` | 1 |
| After 20 concurrent `GET /health` | 1 |
| After full pytest suite (22 tests, each builds its own engine) | 1 |

Pool reuses one persistent connection; no leak after sustained load and explicit test engines.

Task 0.4 test suite run with proper `TEST_DATABASE_URL`/`TEST_REDIS_URL` env vars: **22 passed** in 3.46s.

## 9. Full Cycle Persistence Test — PASS

Sequence executed:

1. `docker compose down -v` — destroyed `postgres_data` and `redis_data` volumes.
2. `docker compose up -d` — fresh services.
3. `alembic upgrade head` — 14 tables created from scratch.
4. Inserted canary row: `users(email='canary@persist.com', balance_micro_rub=12_345_678)`.
5. `docker compose down` (no `-v`) — preserve volumes.
6. `docker compose up -d` — services restored.
7. Verified canary row still present, `system_settings` still has 13 rows, 14 tables still exist.
8. `GET /api/v1/health` → 200 healthy.

Persistence works; migration is reproducible from empty volume.

---

## Acceptance Criteria

- [x] All 9 section tests pass
- [x] No real warnings in docker compose logs (only uvicorn `--reload` info-warnings)
- [x] No connection leaks (pg_stat_activity stable at 1)
- [x] Database constraints enforced at DB layer
- [x] Full restart cycle works (`down -v → up → down → up`)
- [x] All 14 tables exist (note: SCHEMA.md §2 was corrected from "13" to "14" during Task 0.5 — §3 had always defined 14)
- [x] All 13 seed rows present
- [x] All 7 currency functions correct
- [x] DI works without leaks
- [x] This document written

## Outstanding Items (Non-Blocking)

- **ROADMAP Issue #4** — `usage_logs.conversation_id` currently uses default `NO ACTION`; SCHEMA.md §3.8 doesn't declare `ON DELETE SET NULL`. Migration 001 mirrors the spec. Worth resolving before Task 0.7 (conversations CRUD) so the §6 hard-delete retention path doesn't break.
- **Task 0.4 conftest defaults** use `localhost` URLs that don't resolve inside the backend container. Tests pass when run with `TEST_DATABASE_URL`/`TEST_REDIS_URL` env vars set to Docker service names. Worth either making the defaults Docker-aware or documenting the env-var override in CLAUDE.md.
- **Task 0.6 acceptance criteria** had three numeric/labeling errors that contradicted SCHEMA.md §8.2 (e.g. `micro_usd_to_micro_rub(3_000_000, 82_500_000)` was stated as `82500` instead of `247_500_000`). Implementation follows SCHEMA.md (source of truth); flagged for ROADMAP edit.

---

**Conclusion:** Foundation is solid. Proceed to Phase 1 (Auth).
