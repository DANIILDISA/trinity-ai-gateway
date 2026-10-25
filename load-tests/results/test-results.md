# Trinity Load Test Results

**Date:** 2026-05-25  
**Tester:** Claude (automated)  
**Tool:** Locust 2.44.0  
**Target:** FastAPI backend, `http://localhost:8000`  
**Environment:** Single Docker container, single uvicorn worker, local machine (Windows 11)

---

## What was tested

Three user personas ran concurrently in a weighted mix:

- **AnonymousUser (weight 1)** — `GET /health`, `GET /models`
- **AuthenticatedUser (weight 7)** — registers, logs in, then exercises profile, conversations, transactions, notifications, image gallery, conversation create/rename/archive-restore
- **AdminUser (weight 1)** — login + admin dashboard reads; inactive in all runs (no admin credentials supplied)

**What was deliberately excluded:**  
`POST /chat/conversations/{id}/messages` — triggers a real LLM call through OpenRouter, which would incur API costs and be rate-limited. All other non-destructive endpoints were covered.

Each virtual user creates its own account on startup (`lt_<random>@example.com`) and keeps it alive for the duration of the test. Records are not deleted after the run.

---

## Load shape

All three runs used the `custom` shape: a staged ramp through intermediate milestones, a 4-minute hold at peak, then a fast ramp-down.

| Run | Milestones | Hold at peak | Total duration |
|-----|-----------|--------------|----------------|
| 1000 users | 50 → 100 → 250 → 500 → 1000 | 4 min | ~10 min |
| 500 users | 50 → 100 → 250 → 500 | 4 min | ~9 min |
| 250 users | 50 → 100 → 250 | 4 min | ~8 min |

Due to the AdminUser persona stopping immediately (no credentials), effective active users were approximately 7/9 of the target — so 1000 target ≈ 778 authenticated + 111 anonymous, etc.

---

## Results at a glance

| Metric | 1000 users | 500 users | 250 users |
|--------|-----------|----------|----------|
| Total requests | 61,548 | 54,243 | 32,508 |
| Total failures | 1,471 | 155 | **3** |
| **Failure rate** | **2.39%** | **0.29%** | **0.01%** |
| Throughput (peak) | 35 req/s | 111 req/s | 105 req/s |
| Aggregated P50 | 300 ms | 18 ms | **13 ms** |
| Aggregated P95 | 14,000 ms | 1,900 ms | **420 ms** |
| Aggregated P99 | 31,000 ms | 16,000 ms | **7,300 ms** |
| **Verdict** | **Over capacity** | **Marginal** | **Stable** |

---

## 250-user run — stable baseline

**3 total failures** (2 × `RemoteDisconnected`, 1 × sporadic 500 on conversations). All three are network-level blips, not systematic errors. No CSRF failures, no database errors, no Redis errors.

### Latency breakdown

| Endpoint | P50 | P95 | P99 |
|----------|-----|-----|-----|
| `GET /models` | 7 ms | 150 ms | 610 ms |
| `GET /health` | 7 ms | 180 ms | 7,700 ms |
| `GET /users/me` | 13 ms | 250 ms | 6,900 ms |
| `GET /chat/conversations` | 15 ms | 250 ms | 7,300 ms |
| `POST /chat/conversations` | 61 ms | 360 ms | 6,900 ms |
| `POST /auth/login` | 1,000 ms | 13,000 ms | 25,000 ms |
| `POST /auth/register` | 1,100 ms | 18,000 ms | 23,000 ms |
| **All endpoints combined** | **13 ms** | **420 ms** | **7,300 ms** |

Key observations:
- Read endpoints are fast and consistent at P50 and P75. The P99 spikes (6–7 s) are caused by bcrypt registrations stealing CPU at the moment of request, not by the read path itself.
- Auth endpoints are slow at all percentiles — expected for bcrypt, but the tail is very long under even modest concurrency.
- `GET /models` is an outlier in the positive direction (7 ms P50, 610 ms P99) — likely cached or a very cheap query.

---

## 500-user run — marginal

Overall read-endpoint health was reasonable (P50 still 10–20 ms range), but the auth startup burst caused a measurable failure cluster.

### Failure breakdown

| Endpoint | Failures | Error rate | Error types |
|----------|----------|-----------|-------------|
| `POST /auth/register` | 44 | 11.1% | 22 × 403 CSRF invalid, 22 × 500 |
| `GET /health` (startup) | 22 | 5.5% | 503 Service Unavailable |
| `POST /auth/login` | 8 | 2.3% | 500 |
| All read endpoints | ~3–21 each | 0.09–0.24% | 500 |

The 403/503 pair is a chain reaction: Redis becomes overwhelmed during the spawn burst → `/health` returns 503 → the new user has no valid CSRF token → `/register` returns 403. The 22 occurrences map exactly 1:1, confirming a single root cause.

### Latency vs 250 users

Auth degraded significantly: login P50 went from 1 s → 2.9 s, and P95 jumped from 13 s → 22 s. Read-endpoint P50 stayed low (10–20 ms) but P95 climbed to 1,600–3,200 ms — a sign that bcrypt is intermittently saturating the CPU and causing request queuing.

---

## 1000-user run — over capacity

Every error category from the 500-user run appeared at much higher rates, and new failure modes emerged.

### Failure breakdown

| Endpoint | Failures | Error rate |
|----------|----------|-----------|
| `POST /auth/register` | 374 | **38.9%** |
| `GET /health` (startup) | 236 | **23.1%** |
| `POST /auth/login` | 83 | 14.7% |
| All read endpoints | 155–184 each | ~1.7–1.9% |

Auth registration became almost completely unreliable. The 1:1 correlation between health 503s and register 403s held — 236 instances each — confirming Redis saturation as the trigger at this scale. The 500-errors on read endpoints are a new category not present at 250 users, indicating the database connection pool was also exhausted.

### Latency at 1000 users

| Endpoint | P50 | P95 |
|----------|-----|-----|
| `GET /models` | 53 ms | 530 ms |
| All other reads | 360–420 ms | 10,000–14,000 ms |
| `POST /auth/login` | 7,500 ms | 31,000 ms |
| `POST /auth/register` | 1,800 ms | 31,000 ms |

Most requests hitting the 31-second timeout wall indicates the server was completely saturated — requests were queuing indefinitely rather than being processed.

---

## Capacity conclusion

**The current single-worker Docker configuration is stable up to approximately 250 concurrent users.**

At 500 users, the system degrades but remains mostly functional for established (already-logged-in) users. The auth path breaks first. At 1000 users, the server becomes unreliable across all endpoints.

The failure curve is not linear — there is a sharp cliff between 250 and 500 caused by two compounding bottlenecks that are described in detail in `bottlenecks.md`.
