# Load Test Results — Phase 0+1 Fixes

**Date:** 2026-05-26
**Tool:** Locust 2.44.0 (headless, `custom` shape, `LT_MAX_USERS=1000`)
**Target:** FastAPI backend, `http://localhost:8000`
**Environment:** Single Docker container, **single uvicorn worker** (`--reload`) — identical to the 2026-05-25 baseline, so this isolates the Phase 0+1 *code* fixes (bcrypt thread-offload, persistent CSRF Redis client, `last_active_at` throttle, `pool_timeout`→503, `/health` split). Multi-worker / PgBouncer (Phase 2) **not** applied.

## Config

- One run, `custom` shape: ramp 50→100→250→500→1000, hold peak 4 min, fast ramp-down (~10 min total).
- Same weighted persona mix as baseline (Anonymous 1 / Authenticated 7 / Admin 1; admin inactive — no creds).
- `POST /chat/.../messages` (real LLM call) excluded, same as baseline.
- Peak concurrent users reached: **889** (baseline: 887).

## Key metrics — this run vs 2026-05-25 baseline (1000-user run)

| Endpoint | RPS | P50 | P95 | P99 | P99 baseline | Err % | Err % base |
|----------|-----|-----|-----|-----|--------------|-------|-----------|
| GET /models | 49.4 | 36 ms | 230 ms | **350 ms** | 1,000 ms | 0.10% | 0.03% |
| GET /users/me | 41.5 | 640 ms | 2,300 ms | **3,800 ms** | 31,000 ms | 0.17% | 1.76% |
| GET /chat/conversations | 34.1 | 640 ms | 2,300 ms | **3,800 ms** | 31,000 ms | 0.10% | 1.76% |
| GET /users/me/notifications | 13.6 | 660 ms | 2,300 ms | **3,800 ms** | 31,000 ms | 0.14% | 1.89% |
| GET /users/me/transactions | 13.7 | 650 ms | 2,300 ms | **3,700 ms** | 31,000 ms | 0.08% | 1.83% |
| GET /images/gallery | 6.9 | 650 ms | 2,400 ms | **3,900 ms** | 31,000 ms | 0.10% | 1.78% |
| GET /health | 24.8 | 580 ms | 2,200 ms | **3,800 ms** | 31,000 ms | 0.01% | 1.71% |
| POST /auth/login | 1.4 | 230 ms | 2,000 ms | **3,500 ms** | 31,000 ms | **0.00%** | 14.7% |
| POST /auth/register | 1.4 | 300 ms | 2,100 ms | **3,800 ms** | 31,000 ms | **0.00%** | 38.9% |
| POST /chat/conversations | 20.7 | 700 ms | 2,400 ms | **3,900 ms** | 31,000 ms | 0.11% | 1.98% |
| **Aggregated** | **210** (avg) | **310 ms** | **2,100 ms** | **3,600 ms** | **31,000 ms** | **0.11%** | **2.39%** |

- **Peak RPS: 360.9** (baseline peak 277; baseline collapsed to ~35 RPS during the saturated hold — this run did not collapse).
- **Total requests: 117,727** (baseline 61,548) — ~1.9× more work done in the same window.
- **Max response time: 10.0 s** (baseline 32.6 s) — `pool_timeout=10` caps the hang instead of riding to the 31 s client wall.

## Failures

**124 total / 117,727 (0.11%)** — vs baseline **1,471 / 61,548 (2.39%)**.

- 0 × CSRF 403 (baseline: 22–236) — the 503→403 cascade is **eliminated**.
- 0 × auth failures (baseline: 374 register + 83 login).
- ~110 × 500 (DB pool) clustered in a single 1–2 s window at the 500→1000 ramp step (19:19:31–32), then immediate recovery.
- 14 × 503 (clean overload signal: 12 on POST /chat/conversations, 2 on /health) + ~11 network blips (RemoteDisconnected / ConnectionAborted).

## Conclusions

- **Auth no longer collapses.** Login/register P99 dropped from **31 s → ~3.5–3.8 s** with **zero** auth failures (was 38.9% register / 14.7% login). The bcrypt thread-offload removed the event-loop freeze — the single biggest root cause.
- **The 503→403 CSRF cascade is gone** (0 × 403). Persistent Redis client + an unfrozen loop fixed the bootstrap path; `/health` 503s fell from 236 → 2.
- **Tail latency improved 8–9×** across every read endpoint (P99 31 s → ~3.8 s) and error rate fell **23×** (2.39% → 0.11%), while throughput rose (~62k → 118k requests, peak 277 → 361 RPS).
- **Median rose slightly** (≈390 → 640 ms on reads) because the server now genuinely services ~1.9× the load instead of freezing — sub-second median at 889 concurrent users is the healthy tradeoff for an 8× better tail.
- **Remaining bottleneck = DB pool on a single worker.** The only residual failures (~110 × 500) are a 1–2 s pool-exhaustion blip at the 500→1000 step. This is exactly what **Phase 2** targets: multiple workers + larger/ PgBouncer-fronted connection pool. The system is now comfortably stable to ~500–800 users and survives 889 with a brief transient at the top.
