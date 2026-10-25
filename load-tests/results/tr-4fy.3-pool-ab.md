# tr-4fy.3 — DB pool A/B verification (prod-topology gunicorn -w4)

**Date:** 2026-07-01 · **Task:** tr-4fy.3 (H1) · **Branch taken:** **B (defensive-headroom-safe)** — the expected outcome
**Setup:** `docker-compose.pool-test.yml` override — backend `gunicorn -w4` (TrinityUvicornWorker, 4 workers pid 6–9),
pool via env (A/B, no code change), harness `load-tests/pool_repro.py` run inside the container (CPU shared, constant
across arms). Dev stack (postgres + pgbouncer + redis) reused; no `down -v`. `query_wait_timeout=35`, `pool_timeout=10s`,
`limit_concurrency=200`/worker.

## Result: PASS — `pool_timeout` 503s = 0 in every run, both arms

| Arm (pool) | Concurrency×waves | Total | 200 | 503 (code) | **pool_timeout** | p50/p95/p99 ms |
|------------|-------------------|-------|-----|------------|------------------|----------------|
| OLD 20/10  | 400×6  | 2400  | 2400 | 0            | **0** | 384 / 633 / 678 |
| OLD 20/10  | 800×5  | 4000  | 3336 | 664 unparseable | **0** | 726 / 920 / 974 |
| OLD 20/10  | 1600×4 | 6400  | 3163 | 3237 unparseable | **0** | 1679 / 1874 / 1941 |
| NEW 25/15  | 800×5  | 4000  | 3179 | 821 unparseable | **0** | 735 / 949 / 1026 |
| NEW 25/15  | 1600×4 | 6400  | 2996 | 3404 unparseable | **0** | 1667 / 1923 / 2044 |
| NEW 25/15  | 1600×25 (sustained) | 40000 | 19214 | 20786 unparseable | **0** | 1683 / 1937 / 2045 |

**Pass criterion (`pool_timeout` count == 0 on the new pool): met.** Also met on the OLD pool — the DB pool was never the
bottleneck at any feasible concurrency, so there is nothing to "make disappear" (true repro-then-gone was not reachable;
Branch B, as the plan anticipated).

## Why Branch B (no reproducible pool exhaustion)

1. **C1 already removed pool pressure.** Post tr-sgz/tr-ans/tr-9wq, `GET /users/me` acquires and returns its DB connection
   in ~ms (one PK SELECT + a throttled Redis touch). By Little's Law, exhausting a 4×(20+10)=120-slot pool would need
   arrival rates far beyond what asyncio.gather can generate; the pre-C1 "889→22×503" baseline required connections held
   across streaming, which no longer happens.
2. **uvicorn `limit_concurrency=200`/worker sheds first.** The 503s observed at 800/1600 concurrency are `unparseable`
   (a plain-text "Service Unavailable" from uvicorn's global concurrency cap = 4×200 = 800 in-flight ceiling — *not* the
   app's JSON `{"error":{"code":...}}` envelope, hence unparseable). This intended backpressure caps in-flight requests
   **before** the DB pool can be exhausted. The pool bump is therefore invisible to this probe — both arms shed similarly.

## Connection budget held (sampled under sustained 1600-concurrent NEW-pool load)

- **Postgres backends: peaked ~106 < `max_connections=200`.** By state at rest after load: 100 idle + 1 active + 5 internal.
- **PgBouncer `SHOW POOLS` (trinity/trinity, transaction mode): sv (server) conns = 100 = `default_pool_size`, never > 120
  (100 + `reserve_pool_size=20`).** During peak, excess app clients showed as `cl_waiting` (queued for a server slot) —
  the intended "queue, don't 503" behavior. The app-side 4×40=160 client conns multiplex safely onto ≤100 PgBouncer
  server conns; Postgres is not over-subscribed.
- CPU: backend idle ~0.7%; harness runs in-container (competes equally in both arms, so the A/B comparison is unaffected).

## Conclusion

The bump 20/10 → 25/15 is verified **safe defensive headroom** for a 1000+ concurrent prod topology: it raises per-worker
DB slots without breaching the PgBouncer server-conn ceiling (120) or Postgres `max_connections` (200), and introduces no
`pool_timeout` regression. Combined with `query_wait_timeout=35` (> `request_timeout=30`, so a PgBouncer-queue timeout
degrades to a clean 503 via the middleware rather than an unhandled `OperationalError`→500), the change hardens the
saturation path. The authoritative full 250/500/1000 matrix on the true prod image (and host-side harness) remains
**tr-4fy.2**; per-endpoint p95/p99 budget assertions remain **tr-4fy.5**.
