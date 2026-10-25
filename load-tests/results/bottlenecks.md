# Bottleneck Analysis

This file interprets the failure patterns observed in the load tests. Three distinct bottlenecks were identified, each with a clear signature in the error report and latency data.

---

## Bottleneck 1 — CSRF middleware opens a fresh Redis connection on every request

### Signature in the logs

At 500 users the error report contained exactly 22 of each:

```
GET [setup] GET /health: HTTPError('503 Server Error: Service Unavailable')
POST POST /auth/register: HTTPError('403 Client Error: Forbidden')
```

At 1000 users this rose to exactly 236 of each — a perfect 1:1 ratio in both runs. The symmetry is not coincidental.

### What is happening

The CSRF middleware instantiates a Redis client on every incoming HTTP request, including the simplest `GET /health`. Under moderate concurrency this is invisible, but once hundreds of users hit the server simultaneously — particularly during the spawn burst when every new user calls `/health` in their `on_start` method — the Redis connection pool is exhausted. The middleware catches the `RedisError` and returns a 503. The new user, having received a 503 instead of a valid CSRF token, then attempts to `POST /auth/register` with no valid token and receives a 403.

This is a startup amplification problem: all virtual users spawn within a short window, all call `/health` to bootstrap their CSRF token, and all of them race for Redis connections at the same moment. At 250 users the burst is small enough that Redis absorbs it; at 500 the pool saturates.

### Why it matters beyond auth

Every mutating request (POST, PATCH, DELETE) runs two Redis operations inside the middleware: one to validate the `csrf_token` cookie and one to validate the `X-CSRF-Token` header. At 1000 concurrent users making write requests, this doubles or triples the effective Redis load compared to what a simple connection-count estimate would suggest.

---

## Bottleneck 2 — bcrypt runs synchronously on the async event loop

### Signature in the logs

Auth latency escalation across the three runs:

| Metric | 250 users | 500 users | 1000 users |
|--------|----------|----------|-----------|
| Login P50 | 1,000 ms | 2,900 ms | 7,500 ms |
| Register P50 | 1,100 ms | 2,000 ms | 1,800 ms* |
| Login P95 | 13,000 ms | 22,000 ms | 31,000 ms |

*Register P50 appears lower at 1000 because most registrations failed before completing; the successes were ones that got through early.

At 250 users, read-endpoint P99 was already elevated (6,900–7,700 ms) despite P50 being 7–15 ms — a gap that indicates intermittent CPU starvation rather than a steady load problem.

### What is happening

bcrypt password hashing is CPU-intensive by design. In an async Python process, a CPU-bound operation that runs on the main thread blocks the entire event loop for its duration — no other request can be handled while a hash is being computed. A single bcrypt hash at typical work factors takes 100–600 ms on this hardware. When multiple users register or log in at the same time, their hashing operations queue up sequentially on the same thread. At 500+ users all registering within a 60-second spawn window, the event loop is effectively locked for seconds at a time.

This explains why read endpoints (which are I/O-bound and fast) see P99 values in the 7,000–31,000 ms range even though their P50 is 10–15 ms: they are occasionally stuck behind a bcrypt operation that has taken control of the event loop.

### Compounding effect

bcrypt and the CSRF Redis issue interact. A user whose `/health` call was delayed by bcrypt CPU pressure is more likely to race with the registration burst and hit the Redis exhaustion window. The two bottlenecks make each other worse.

---

## Bottleneck 3 — PostgreSQL connection pool exhaustion

### Signature in the logs

At 1000 users, 500 errors appeared on every DB-backed read endpoint — a category absent from the 250-user run entirely:

```
GET GET /users/me: HTTPError('500 Server Error: Internal Server Error') — 184 occurrences
GET GET /chat/conversations: HTTPError('500 Server Error')              — 155 occurrences
GET GET /users/me/notifications: HTTPError('500 Server Error')          —  64 occurrences
GET GET /users/me/transactions: HTTPError('500 Server Error')           —  63 occurrences
```

At 500 users the same 500 errors appeared but in much smaller numbers (19, 19, 3, 5 respectively), suggesting the pool was occasionally exhausted during bursts rather than continuously.

### What is happening

The SQLAlchemy async engine has a finite pool of database connections. When every request requires a connection, a large number of simultaneous requests will exhaust the pool. Requests that cannot acquire a connection within the pool timeout receive an exception, which the application translates into a 500. At 1000 users with read tasks running at high frequency, the pool ceiling is hit continuously. At 500 users it is hit intermittently during traffic spikes. At 250 users the pool is sufficient.

The error category "Internal Server Error" rather than a specific timeout or pool error in the HTTP response is because the application does not distinguish pool exhaustion from other database errors in its error handling — both surface as 500.

---

## How the bottlenecks interact

The three bottlenecks reinforce each other in a cascade:

1. A large spawn burst sends hundreds of users to `/health` simultaneously → Redis pool exhausts → some users fail to get CSRF tokens → they cannot register → they stop (`StopUser`) → those slots are immediately refilled by Locust → another burst hits `/health` → cycle repeats.

2. Concurrent bcrypt operations during the same spawn burst lock the event loop → Redis operations that are waiting for the event loop to free up time out → Redis pool appears exhausted even if it has available connections.

3. With the event loop locked by bcrypt, DB operations for read-endpoint requests queue up → the DB pool appears exhausted from the application's perspective even if connections are available at the PostgreSQL level, because no connection can be acquired while the loop is frozen.

The result is that all three bottlenecks share a single root trigger: the synchronous bcrypt operation monopolising the async event loop. Fixing bcrypt offloading would reduce pressure on both the Redis and database pools significantly.

---

## What is NOT a bottleneck

- **The read path itself** — at 250 users, read P50 values of 7–15 ms confirm that routing, middleware (other than CSRF), serialization, and query execution are all healthy.
- **PostgreSQL query performance** — the fast P50 times on read endpoints rule out slow queries as a cause; the problem is connection acquisition, not execution.
- **Network or HTTP layer** — the 3 `RemoteDisconnected` errors in the 250-user run are isolated blips, not a pattern.
- **Redis data operations** — CSRF token lookups themselves are fast; the problem is connection management, not lookup latency.
