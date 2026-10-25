# Load Test Results — Phase 3 (lock-window shortening + request-timeout middleware + chat semaphore)

**Date:** 2026-05-27
**Tool:** Locust 2.44.0 (headless, `custom` shape, `LT_MAX_USERS=1000`) — identical params to `results_phase02_20260527.md`.
**Target:** FastAPI backend, `http://localhost:8000`.

## Environment note

Same as Phase 2: dev compose runs the backend as a **single uvicorn `--reload` worker** behind PgBouncer (transaction pooling) + tuned Postgres. `POST /chat/.../messages` (real LLM) excluded. `gunicorn -w 4` is prod-image only and **not** exercised here. Peak concurrent users: **895** (Phase 2: 889).

Phase 3 code deltas vs Phase 2: (1) §5.1 dropped the STEP-1 user `FOR UPDATE` + `populate_existing` in `create_reservation` — **`/messages` path only, excluded from this test**; (2) **M3 `RequestTimeoutMiddleware`** — a new `BaseHTTPMiddleware` wrapping **every** non-stream request in `asyncio.wait_for(call_next, 30s)`; (3) chat semaphore — not stressed (`/messages` excluded).

So the only Phase-3 change touching the tested endpoints is the request-timeout middleware.

## ⚠️ Headline: REGRESSION vs Phase 2

| Metric | **Phase 3** | Phase 2 | Δ |
|--------|-------------|---------|---|
| Peak RPS | **272** | 338 | −20% |
| Avg RPS | 165 | 207 | −20% |
| Total requests | 92,402 | 115,652 | −20% |
| **Total failures** | **83 (0.09%)** | 7 (0.01%) | **+76, 9× worse** |
| Max response | 10,449 ms | 9,300 ms | +12% |
| Aggregate P50 | **1,100 ms** | 600 ms | +83% |
| Aggregate P95 | **4,300 ms** | 2,000 ms | +115% |
| Aggregate P99 | **6,500 ms** | 3,200 ms | +103% |

## Per-endpoint P99 — Phase 3 vs Phase 2

| Endpoint | RPS | P50 | P95 | **P99** | P99 (P02) | Err % | Err % (P02) |
|----------|-----|-----|-----|---------|-----------|-------|-------------|
| GET /models | 38.5 | 37 ms | 330 ms | **2,300 ms** | 300 ms | 0.02% | 0.01% |
| GET /users/me | 32.0 | 1,800 ms | 4,700 ms | **6,900 ms** | 3,300 ms | 0.12% | 0.01% |
| GET /chat/conversations | 26.8 | 1,800 ms | 4,600 ms | **7,000 ms** | 3,400 ms | 0.15% | 0.01% |
| GET /users/me/notifications | 10.6 | 1,800 ms | 4,600 ms | **6,600 ms** | 3,300 ms | 0.13% | 0.00% |
| GET /users/me/transactions | 10.7 | 1,800 ms | 4,600 ms | **6,600 ms** | 3,400 ms | 0.10% | 0.00% |
| GET /images/gallery | 5.4 | 1,800 ms | 4,600 ms | **7,000 ms** | 3,600 ms | 0.10% | 0.00% |
| GET /health | 19.4 | 1,500 ms | 4,400 ms | **6,500 ms** | 3,200 ms | 0.05% | 0.00% |
| POST /auth/login | 1.4 | 320 ms | 3,900 ms | **6,000 ms** | 2,800 ms | 0.13% | 0.00% |
| POST /auth/register | 1.4 | 430 ms | 3,600 ms | **5,900 ms** | 3,100 ms | 0.00% | 0.00% |
| POST /chat/conversations | 16.0 | 1,900 ms | 4,900 ms | **7,200 ms** | 3,500 ms | 0.11% | 0.11% |
| **Aggregated** | **272 peak** | **1,100 ms** | **4,300 ms** | **6,500 ms** | **3,200 ms** | **0.09%** | **0.01%** |

## Failures — 83 total / 92,402 (0.09%)

- **75 × HTTP 503** spread across DB-backed endpoints (users/me 21, chat/conversations 21+9, notifications 8, health 5, transactions 4, models 3, gallery 3, login 1).
- 8 × network blips (7 × `RemoteDisconnected`, 1 × `ConnectionAbortedError`).

The 503s land on **GET** endpoints, whose only 503 path is the exception handler — and max response clamps at ~10 s = the `pool_timeout=10s`. So these are **DB connection-pool timeouts**: the per-worker pool (12+6=18) exhausted at peak. Backend logs confirm 76 × `503 Service Unavailable` with no tracebacks (consistent with the pool-timeout→503 handler). PgBouncer showed no saturation (server connections reused normally) — the exhaustion is the app-side SQLAlchemy pool, not Postgres/PgBouncer.

## Root cause: the request-timeout middleware

The single worker was already near saturation in Phase 2 (P99 3.2 s at 889 users). Phase 3 adds `RequestTimeoutMiddleware` as a **4th `BaseHTTPMiddleware`** layer — each such layer runs the downstream app in its own task and pumps the response over an anyio memory stream, and `asyncio.wait_for` wraps every call in another task. On an already-near-100%-utilized event loop, that extra per-request service time amplifies non-linearly (queueing theory): aggregate latency ~doubled, requests held their DB connections longer, the 18-slot pool exhausted, and waiters hit the 10 s `pool_timeout` → 503.

The timeout's 30 s budget was **never reached** (max response 10.4 s), so the middleware never did its intended job here — it only added overhead. **Net negative on the single worker.**

Caveat: the run shares the host with Docker + the browser session; host contention can inflate latency. But the 503s are genuinely server-side (pool exhaustion), which client slowness would *reduce*, not cause — so the attribution to added server-side per-request cost stands. A confirming re-run with the middleware removed/reimplemented would make it definitive.

## Recommendation

Reimplement `RequestTimeoutMiddleware` as a lightweight **pure-ASGI** middleware (no `BaseHTTPMiddleware` task-hop, no `wait_for` task wrap), or drop it: the existing backpressure (`pool_timeout`→503, chat semaphore, gunicorn limits) already covers overload, and the 30 s net trips so rarely it isn't worth a per-request tax. Then re-run to confirm Phase 2's clean profile (0×503) is restored. The §5.1 lock-shortening and chat semaphore are unaffected by this and stay.

## Data integrity (safety check)

Aggregate row counts + total balance, before vs after the run:

| | users | conversations | messages | usage_logs | Σ balance (μ₽) |
|--|-------|---------------|----------|------------|----------------|
| PRE | 2,828 | 36,944 | 136 | 73 | 84,422,664 |
| POST | 3,606 | 45,906 | 138 | 74 | 84,422,612 |
| Δ | +778 | +8,962 | +2 | +1 | −52 |

- **users +778** = exactly the 778 `POST /auth/register` calls. **conversations +8,962** ≈ the 8,953 `POST /chat/conversations` calls. Expected growth — the load test creates these by design and deletes nothing.
- **msgs +2 / logs +1 / balance −52 μ₽**: the load test never calls `/messages`, so this is **one incidental real chat** during the 10-min window (a still-open browser session), and it's internally **consistent** — a 52 μ₽ debit matched by exactly one usage_log and its two (user+assistant) messages.
- **No data lost or corrupted:** every registration and conversation persisted; archive/restore left no orphans; the only balance movement is a fully-accounted debit.

**Data integrity: OK.**
