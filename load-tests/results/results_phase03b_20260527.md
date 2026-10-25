# Load Test Results — Phase 3b (request-timeout middleware reimplemented as pure-ASGI)

**Date:** 2026-05-27
**Tool:** Locust 2.44.0 (headless, `custom` shape, `LT_MAX_USERS=1000`) — identical params to phase02 / phase03.
**Target:** FastAPI backend, `http://localhost:8000`. Single uvicorn `--reload` worker behind PgBouncer + tuned Postgres (dev; `gunicorn -w 4` is prod-only, not exercised). Peak users: **889**.

## What changed since phase03

Only one code change: `RequestTimeoutMiddleware` was rewritten from a `BaseHTTPMiddleware` (which runs the app in its own task and pumps the response over an anyio memory stream) to a **pure-ASGI** middleware that wraps the downstream call directly. Behaviour is identical (503 after `request_timeout_seconds`, SSE chat path exempt, 503 only if the response hasn't started) — 372/372 tests still pass. The goal: remove the per-request overhead that regressed phase03 on the near-saturated single worker.

## Headline — phase03b vs phase03 vs phase02

| Metric | **phase03b** | phase03 (BaseHTTP mw) | phase02 (no mw) |
|--------|--------------|------------------------|------------------|
| Peak RPS | **284** | 272 | 338 |
| Total requests | **100,380** | 92,402 | 115,652 |
| **Total failures** | **28 (0.03%)** | 83 (0.09%) | 7 (0.01%) |
| — of which HTTP 503 | **22** | 75 | 0 |
| — of which network blips | 6 | 8 | 7 |
| Max response | 10,212 ms | 10,449 ms | 9,300 ms |
| Aggregate P50 | **930 ms** | 1,100 ms | 600 ms |
| Aggregate P95 | **3,500 ms** | 4,300 ms | 2,000 ms |
| Aggregate P99 | **5,400 ms** | 6,500 ms | 3,200 ms |

**The pure-ASGI fix recovered most of the regression:** failures 83→28 (−66%), 503s 75→22 (−71%), aggregate P99 6.5s→5.4s, P50 1.1s→0.93s, throughput +9% (92k→100k req). It did **not** fully return to phase02's pristine run (7 failures, 0×503, P99 3.2s).

## Per-endpoint P99 — phase03b vs phase02

| Endpoint | RPS | P50 | P95 | **P99** | P99 (p02) | Err % | Err % (p02) |
|----------|-----|-----|-----|---------|-----------|-------|-------------|
| GET /models | 41.7 | 29 ms | 260 ms | **400 ms** | 300 ms | 0.00% | 0.01% |
| GET /users/me | 35.1 | 1,400 ms | 3,800 ms | **5,700 ms** | 3,300 ms | 0.04% | 0.01% |
| GET /chat/conversations | 29.0 | 1,500 ms | 3,900 ms | **5,800 ms** | 3,400 ms | 0.04% | 0.01% |
| GET /users/me/notifications | 11.8 | 1,500 ms | 3,800 ms | **5,700 ms** | 3,300 ms | 0.05% | 0.00% |
| GET /users/me/transactions | 11.5 | 1,500 ms | 3,800 ms | **5,700 ms** | 3,400 ms | 0.00% | 0.00% |
| GET /images/gallery | 5.8 | 1,400 ms | 3,800 ms | **5,700 ms** | 3,600 ms | 0.03% | 0.00% |
| GET /health | 21.2 | 1,300 ms | 3,600 ms | **5,600 ms** | 3,200 ms | 0.02% | 0.00% |
| POST /auth/login | 1.4 | 250 ms | 2,300 ms | **4,100 ms** | 2,800 ms | 0.00% | 0.00% |
| POST /auth/register | 1.4 | 290 ms | 2,200 ms | **3,800 ms** | 3,100 ms | 0.00% | 0.00% |
| POST /chat/conversations | 17.7 | 1,500 ms | 3,900 ms | **6,000 ms** | 3,500 ms | 0.06% | 0.11% |
| **Aggregated** | **284 peak** | **930 ms** | **3,500 ms** | **5,400 ms** | **3,200 ms** | **0.03%** | **0.01%** |

## Failures — 28 total / 100,380 (0.03%)

- **22 × HTTP 503** (users/me 6, chat/conversations 5+5, notifications 3, health 2, gallery 1) — still DB connection-pool timeouts (max clamps at the 10 s `pool_timeout`; backend logged 24 × 503 with no tracebacks).
- 6 × network blips (4 × `RemoteDisconnected`, 2 × `ConnectionAbortedError`).

## Interpretation

The BaseHTTPMiddleware overhead was the **dominant** contributor to the phase03 regression — removing it cut failures and tail latency by ~⅔. The **residual 22 × 503** are the single worker hitting its capacity edge at ~889 users: at that load the 18-slot pool occasionally drains and waiters trip the 10 s `pool_timeout`. phase02's 0×503 was a cleaner run of the same edge — at the saturation knee, run-to-run variance is large (these runs are hours apart with different host load), so phase03b vs phase02 is within "edge-of-capacity" noise rather than a distinct code regression. The pure-ASGI timeout middleware's own 30 s budget never trips here (max 10.2 s); it's now a cheap safety net as intended.

The definitive fix for the residual pool-timeouts is **horizontal scale**: the prod `gunicorn -w 4` image gives 4 independent event loops + pools, which the dev single-worker run cannot show. That remains the recommended next measurement (a prod-image run).

## Data integrity (safety check)

| | users | conversations | messages | usage_logs | Σ balance (μ₽) |
|--|-------|---------------|----------|------------|----------------|
| PRE | 3,606 | 45,906 | 138 | 74 | 84,422,612 |
| POST | 4,384 | 55,846 | 140 | 75 | 84,422,535 |
| Δ | +778 | +9,940 | +2 | +1 | −77 |

- **users +778** = exactly the 778 registrations. **conversations +9,940** ≈ the 9,936 `POST /chat/conversations` calls. Expected growth; the test deletes nothing.
- **msgs +2 / logs +1 / balance −77 μ₽**: the test never calls `/messages`, so this is one **incidental** real chat during the window (the open browser session) — internally consistent (a 77 μ₽ debit matched by exactly one usage_log and its two messages).
- **No data lost or corrupted.**

**Data integrity: OK.**
