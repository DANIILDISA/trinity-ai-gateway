# Load Test Results — Phase 2 (PgBouncer + Postgres tuning + multi-worker code)

**Date:** 2026-05-27
**Tool:** Locust 2.44.0 (headless, `custom` shape, `LT_MAX_USERS=1000`) — identical params to `results_phase01_20260526.md`.
**Target:** FastAPI backend, `http://localhost:8000`.

## Environment note (important for reading the numbers)

The dev compose still runs the backend as a **single uvicorn `--reload` worker** (Phase 2 keeps `--reload` for dev by design; `gunicorn -w 4` is the **prod-image** command only). So this run does **not** exercise multi-worker parallelism. What Phase 2 *does* change for this dev run:

- **PgBouncer** (transaction pooling) now fronts Postgres — app → `pgbouncer:6432` → `postgres`.
- **Postgres tuned** — `max_connections=200`, `shared_buffers=256MB`, `work_mem=4MB`, `effective_cache_size=1GB`.
- **Per-worker pool** `12+6` (was `20+10`), multiplexed by PgBouncer.
- **Chat-stream semaphore** (50/worker) — not stressed here (`/messages` excluded).

So the headline to expect is **reliability**, not peak throughput: the single-worker RPS ceiling is unchanged, but the DB-pool exhaustion that caused Phase 01's residual 500s is gone.

## Config

- One run, `custom` shape: ramp 50→100→250→500→1000, hold peak 4 min, ramp-down (~10 min).
- Same weighted personas (Anon 1 / Auth 7 / Admin 1; admin inactive — no creds).
- `POST /chat/.../messages` (real LLM call) excluded — same as Phase 01.
- **Peak concurrent users: 889** (Phase 01: 889).

## Key metrics — Phase 2 vs Phase 01

| Endpoint | RPS | P50 | P95 | **P99** | P99 (P01) | Err % | Err % (P01) |
|----------|-----|-----|-----|---------|-----------|-------|-------------|
| GET /models | 48.4 | 25 ms | 200 ms | **300 ms** | 350 ms | 0.01% | 0.10% |
| GET /users/me | 40.5 | 820 ms | 2,100 ms | **3,300 ms** | 3,800 ms | 0.01% | 0.17% |
| GET /chat/conversations | 33.7 | 820 ms | 2,200 ms | **3,400 ms** | 3,800 ms | 0.01% | 0.10% |
| GET /users/me/notifications | 13.7 | 820 ms | 2,200 ms | **3,300 ms** | 3,800 ms | 0.00% | 0.14% |
| GET /users/me/transactions | 13.4 | 840 ms | 2,200 ms | **3,400 ms** | 3,700 ms | 0.00% | 0.08% |
| GET /images/gallery | 6.7 | 830 ms | 2,200 ms | **3,600 ms** | 3,900 ms | 0.00% | 0.10% |
| GET /health | 24.8 | 720 ms | 2,000 ms | **3,200 ms** | 3,800 ms | 0.00% | 0.01% |
| POST /auth/login | 1.4 | 240 ms | 1,600 ms | **2,800 ms** | 3,500 ms | 0.00% | 0.00% |
| POST /auth/register | 1.4 | 290 ms | 1,800 ms | **3,100 ms** | 3,800 ms | 0.00% | 0.00% |
| POST /chat/conversations | 20.0 | 870 ms | 2,300 ms | **3,500 ms** | 3,900 ms | 0.01% | 0.11% |
| **Aggregated** | **206.7** (avg) | **600 ms** | **2,000 ms** | **3,200 ms** | **3,600 ms** | **0.01%** | **0.11%** |

| Headline | Phase 2 | Phase 01 |
|----------|---------|----------|
| Peak RPS | **338** | 361 |
| Total requests | 115,652 | 117,727 |
| Total failures | **7 (0.01%)** | 124 (0.11%) |
| Max response time | **9.3 s** | 10.0 s |
| Peak concurrent users | 889 | 889 |

## Failures — 7 total / 115,652 (0.01%)

All 7 are transient client-side network blips, **no server errors**:

- 6 × `RemoteDisconnected` (2 /models, 2 /users/me, 1 /chat/conversations, 1 POST /chat/conversations)
- 1 × `ConnectionAbortedError` (10053) on /users/me

**Zero 500s, zero 503s, zero CSRF 403s, zero auth failures.**

vs Phase 01's 124 failures (~110 × **500 DB-pool exhaustion** at the 500→1000 ramp + 14 × 503 + ~11 network).

## Conclusions

- **The DB-pool bottleneck is eliminated.** Phase 01's residual ~110 × 500 (pool exhaustion blip at the 500→1000 step) is **gone (0 × 500)**. PgBouncer transaction pooling multiplexes the app's connections onto a small, stable set of real Postgres connections, so the pool no longer exhausts even on a single worker. Error rate fell **18×** (0.11% → 0.01%).
- **Tail latency improved further** across every endpoint (aggregate P99 3,600 → 3,200 ms; reads ~3,800 → ~3,300 ms; auth register 3,800 → 3,100 ms), on top of Phase 0+1's 8–9× gain.
- **Peak RPS is ~flat** (361 → 338, run-to-run variance) and that is **expected**: the dev test ran a single uvicorn worker, so the per-process throughput ceiling is unchanged. Phase 2's `gunicorn -w 4` multi-worker parallelism — the actual throughput lever — only runs in the prod image and is **not** measured here. A prod-image run would be needed to quantify the multi-core RPS gain.
- **asyncpg ↔ PgBouncer transaction mode verified clean** under load: no prepared-statement / `DuplicatePreparedStatement` errors in 115k requests, confirming the `statement_cache_size=0` + unique-name connect_args.

## Next step

Run the same shape against the **prod gunicorn image** (4 workers) to measure the multi-worker throughput ceiling — that is the metric this dev-only run cannot show.
