# Recommendations for 1,000–1,500 Concurrent Users

Based on the three load test runs and the bottleneck analysis in `bottlenecks.md`.  
The recommendations are ordered by impact — the first two alone would likely get the system past 1,000 users.

---

## Priority 1 — Offload bcrypt to a thread pool

**Problem:** Password hashing and verification run synchronously on the async event loop, blocking all other requests for 100–600 ms at a time. This is the root cause that amplifies both the Redis and database bottlenecks.

**Recommendation:** Move every bcrypt call inside an executor so it runs in a worker thread rather than on the event loop. FastAPI's async framework provides this natively via `run_in_executor`. The login and registration service functions are the only places this needs to change.

**Expected impact:** Auth P50 drops significantly. Read-endpoint P99 tail latency (currently 7–25 s at high load) should shrink because the event loop is no longer starved. Redis and DB pool pressure decreases as a side effect because fewer operations are queued behind a frozen loop.

**Risk:** Low. The change is localised to two service functions. Behaviour is identical; only the thread context changes.

---

## Priority 2 — Give the CSRF middleware a persistent Redis client

**Problem:** The CSRF middleware creates a new Redis client instance on every HTTP request. Under a concurrent spawn burst, this exhausts the Redis connection pool and causes a 503 → 403 cascade that prevents new users from registering at all. The 1:1 error count correlation (236 health 503s → 236 register 403s at 1000 users) confirms this is a hard failure, not a soft degradation.

**Recommendation:** Replace the per-request Redis instantiation in the CSRF middleware with a single async Redis client that is initialised once at application startup and reused across all requests — the same lifecycle pattern the application already uses for the database session factory. This client should have an explicit `max_connections` setting that reflects realistic concurrency.

**Expected impact:** Eliminates the CSRF failure chain entirely. Registration reliability should reach near-zero failures even at 1,000+ users, assuming Priority 1 is also addressed (otherwise bcrypt still blocks the event loop during auth bursts).

**Risk:** Low. The middleware logic does not change, only where the client comes from.

---

## Priority 3 — Run multiple uvicorn workers

**Problem:** The entire application — including bcrypt CPU work, all async I/O, and all request handling — runs in a single OS process on a single CPU core. No amount of async I/O optimisation can compensate for a CPU-bound operation monopolising a single-process server.

**Recommendation:** Launch uvicorn (or gunicorn with uvicorn workers) with a worker count matched to the number of available CPU cores, typically `2 × cores + 1` for I/O-heavy workloads. Each worker is an independent process with its own event loop, so a bcrypt operation in one worker does not affect any other.

**Expected impact:** Linear throughput scaling with worker count for I/O operations. bcrypt-induced event loop starvation is contained to a single worker rather than affecting the entire server. Database and Redis connections are shared across workers through the pool.

**Note:** Multiple workers require that any in-process state (if any exists) is moved to Redis or the database, and that the connection pools in each worker are sized to avoid overwhelming downstream services collectively. With 4 workers each having a pool of 10, that is 40 simultaneous DB connections from one host — ensure PostgreSQL's `max_connections` can accommodate this.

**Risk:** Medium. Requires verifying that the application has no in-process shared mutable state. Lifespan events and background tasks need to be reviewed to ensure they behave correctly across multiple workers.

---

## Priority 4 — Increase the database connection pool size and add a connection timeout

**Problem:** At 1,000 users, 500 errors appear on every DB-backed read endpoint (184 on `/users/me`, 155 on `/chat/conversations`, etc.), indicating the SQLAlchemy pool is exhausted. The default pool size is too small for this concurrency level.

**Recommendation:**
- Raise `pool_size` to 20–30 and `max_overflow` to 10–20, depending on available PostgreSQL connections.
- Set an explicit `pool_timeout` (e.g., 10 s) so that an exhausted pool returns a clean error quickly rather than leaving requests queued until they hit the uvicorn request timeout.
- Set `pool_pre_ping=True` to detect stale connections before they are handed to a request handler.

**Expected impact:** Eliminates the 500 errors on read endpoints under high concurrency. Combined with multiple workers (Priority 3), each worker gets its own pool, so the per-worker pool can be smaller while the total capacity is larger.

**Risk:** Low. PostgreSQL connection limits on the server side must be checked. The database itself may need its `max_connections` raised in `postgresql.conf` if the total across all workers exceeds the default (typically 100).

---

## Priority 5 — Cache the models list in Redis

**Problem:** `GET /models` is the highest-traffic endpoint (14,768 requests in the 1,000-user run, 7,764 in the 250-user run) and is called by every user in their startup sequence and repeatedly during the session. The model list changes at most once per day via the background sync task.

**Recommendation:** Cache the serialised model list in Redis with a TTL of 5–15 minutes. On a cache hit, skip the database query entirely and deserialise directly from Redis. On a cache miss or TTL expiry, query the database and update the cache. The existing `ModelSyncService` background task is the natural place to invalidate or warm this cache.

**Expected impact:** Removes the most frequent database query from all 1,000+ concurrent users and converts it to a sub-millisecond Redis read. Frees up database connections for endpoints that cannot be cached. At 1,000 users, this alone would reduce DB connection demand by roughly 20–25% based on the observed request distribution.

**Risk:** Low. Stale model data for up to 15 minutes is acceptable given the daily sync cadence. Cache invalidation on manual model changes (via admin endpoints) should be added.

---

## Priority 6 — Add a Redis connection pool limit to the CSRF middleware specifically

**Problem related to Priority 2:** Even with a persistent Redis client, at 1,000–1,500 users the number of concurrent CSRF operations could still be high. Each mutating request (POST, PATCH, DELETE) currently performs two Redis round-trips inside the middleware. This is twice the Redis load of a single check.

**Recommendation:** Consider combining the two Redis checks (cookie validation and header validation) into a single Lua script or pipeline call, reducing two round-trips to one per request. Additionally, reconsider whether the double-token pattern (cookie + header) is necessary given that the API is already protected by short-lived JWT bearer tokens on all authenticated endpoints. For truly stateless API clients, bearer token authentication alone is an established CSRF mitigation.

**Expected impact:** Halves Redis operations on all mutating endpoints. At 1,000 users with heavy write traffic, this could meaningfully reduce Redis connection pressure.

**Risk:** Medium. Changing the CSRF strategy requires a security review to confirm the alternative protection is equivalent.

---

## Priority 7 — Tune uvicorn timeouts and backpressure

**Problem:** Under 1,000-user load, many requests hit the 31-second wall (Locust's default timeout). Rather than timing out gracefully, they consume server resources — event loop slots, open connections, possibly partially-acquired DB connections — for the full 31 seconds before being abandoned.

**Recommendation:**
- Set a `--timeout-keep-alive` on uvicorn that is shorter than the client timeout, so the server proactively closes idle connections.
- Add a request timeout middleware (or use uvicorn's `--timeout-graceful-shutdown`) to return a 503 after a configurable threshold (e.g., 10–15 s) rather than leaving requests pending indefinitely.
- Consider adding a concurrency limiter (e.g., a semaphore on heavy endpoints like auth) that returns a 429 immediately when the server is saturated, rather than queuing until timeout.

**Expected impact:** Faster failure under overload means resources are freed sooner, reducing the cascade effect. A 503 returned in 5 s is more recoverable than a 31-s queue that exhausts the pool.

**Risk:** Low. Timeouts are a safety net, not a load-bearing change. Must be tuned to not interfere with legitimate long-running operations (the SSE streaming endpoint for chat messages would need to be excluded from a global request timeout).

---

## Summary — expected capability after implementing Priorities 1–4

| Bottleneck | Current limit | After fix |
|------------|--------------|-----------|
| bcrypt event loop block | Affects all requests above ~50 concurrent auth ops | Contained to worker threads; no event loop impact |
| Redis CSRF exhaustion | Fails above ~400 concurrent spawns | Eliminated |
| DB pool exhaustion | Fails above ~400–500 concurrent users | Stable to 1,000+ with pool size adjustment |
| Single worker CPU ceiling | One core for all work | Scales with available cores |

The combination of Priorities 1–4 is expected to comfortably support 1,000–1,500 concurrent users on the same hardware, provided the host machine has at least 4 CPU cores available to the Docker container. Priorities 5–7 provide additional headroom and resilience but are not required to cross the 1,000-user threshold.
