# Trinity Load Tests

Load tests for the Trinity AI gateway backend, written with [Locust](https://locust.io/).

---

## Quick start

```powershell
# From the project root:
cd load-tests

# Install dependencies (one-time):
pip install -r requirements.txt

# Run the default ramp test (generates report_ramp.html):
.\run.ps1

# Or with the web UI (live charts at http://localhost:8089):
locust -f locustfile.py --host http://localhost:8000
```

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | ≥ 3.10 | python.org |
| Locust | ≥ 2.24 | `pip install locust` |
| Trinity backend | running | `docker compose up -d` |

The tests hit the backend at `http://localhost:8000` by default.  
The frontend does **not** need to be running.

---

## Load shapes

Select the shape with the `LOAD_SHAPE` env var (or `-Shape` in the script):

| Shape | Description | Duration | Peak users |
|-------|-------------|----------|------------|
| `ramp` *(default)* | Gradual ramp-up to find stable throughput | ~6 min | 50 |
| `steady` | Flat constant load – measures P99 under sustained pressure | ~7 min | 25 |
| `spike` | Sudden 10× spike then recovery – tests resilience | ~6 min | 100 |
| `stress` | Keeps doubling to locate the saturation/breaking point | ~8 min | 150 |

### PowerShell runner

```powershell
.\run.ps1                     # ramp (default)
.\run.ps1 -Shape spike        # spike test
.\run.ps1 -Shape stress       # stress test (finds the breaking point)
.\run.ps1 -Shape steady `
    -Host http://192.168.1.5:8000  # against a remote host
```

### Raw Locust commands

```powershell
# Headless – auto-stops when the shape completes:
$env:LOAD_SHAPE = "ramp"
locust -f locustfile.py --host http://localhost:8000 --headless `
       --html report_ramp.html --csv report_ramp --loglevel WARNING

# Interactive web UI (http://localhost:8089):
locust -f locustfile.py --host http://localhost:8000

# Different shape:
$env:LOAD_SHAPE = "stress"
locust -f locustfile.py --host http://localhost:8000 --headless `
       --html report_stress.html --csv report_stress
```

---

## User personas

Three virtual user types simulate different traffic patterns:

### `AnonymousUser` (weight 1)
Unauthenticated public traffic.
- `GET /api/v1/health`
- `GET /api/v1/models`

### `AuthenticatedUser` (weight 7)
The primary persona. Each virtual user **registers a unique account** on startup
(email `lt_<random12hex>@example.com`) and performs realistic read/write ops.

**Read (high frequency):**
- `GET /users/me`
- `GET /models`
- `GET /chat/conversations`
- `GET /users/me/transactions`
- `GET /users/me/notifications`
- `GET /images/gallery`

**Write (lower frequency):**
- `POST /chat/conversations` (creates a conversation, keeps its ID)
- `GET /chat/conversations/{id}` (read back)
- `PATCH /chat/conversations/{id}` (rename)
- `DELETE + POST /chat/conversations/{id}/restore` (archive then immediately restore)

### `AdminUser` (weight 1)
Admin dashboard reads — **only active** when both env vars are set:
```powershell
$env:LT_ADMIN_EMAIL    = "admin@example.com"
$env:LT_ADMIN_PASSWORD = "..."
```
Reads: `/admin/dashboard`, `/admin/users`, `/admin/alerts`,  
`/admin/dashboard/revenue-by-model`, `/admin/tariffs`.

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LOAD_SHAPE` | `ramp` | Shape to run (`ramp`/`steady`/`spike`/`stress`) |
| `LT_ADMIN_EMAIL` | *(unset)* | Admin email – enables AdminUser persona |
| `LT_ADMIN_PASSWORD` | *(unset)* | Admin password |

---

## What is NOT tested (by design)

| Endpoint | Reason |
|----------|--------|
| `POST /chat/conversations/{id}/messages` | Calls the real LLM (OpenRouter) – would incur API costs and be rate-limited. Not suitable for load testing. |
| `POST /payments/create` | YooKassa integration – cannot be hammered safely. |
| `DELETE /chat/conversations/{id}/permanent` | Permanently destroys data. Excluded per the no-delete policy. |
| `POST /auth/reset-password/*` | Requires email delivery; no-op in dev. |

---

## Database note

Each test run creates `N` user accounts (one per virtual user) plus conversation
records.  These remain in the database after the test — no records are deleted.

If you want a clean slate before a run, truncate the load-test users manually:
```sql
DELETE FROM users WHERE email LIKE 'lt\_%@example.com' ESCAPE '\';
-- (cascades to conversations, messages, etc. via FK ON DELETE CASCADE)
```

---

## Output files

After a headless run the following files appear in `load-tests/`:

| File | Contents |
|------|----------|
| `report_<shape>.html` | **Full interactive report** with request charts and percentile tables |
| `report_<shape>_stats.csv` | Per-endpoint summary: req/s, median, P95, P99, failures |
| `report_<shape>_stats_history.csv` | Time-series: RPS + latencies sampled every 2 s |
| `report_<shape>_failures.csv` | All distinct failure types |
| `report_<shape>_exceptions.csv` | Python-side exceptions (e.g. connection refused) |

---

## Interpreting results

### Key metrics

| Metric | Good | Degraded | Poor |
|--------|------|----------|------|
| Error rate | < 1% | 1–5% | > 5% |
| P50 latency | < 100 ms | 100–500 ms | > 500 ms |
| P95 latency | < 500 ms | 500 ms–2 s | > 2 s |
| P99 latency | < 1 s | 1–5 s | > 5 s |

### Recommended test sequence

1. **`ramp`** – Find the throughput plateau (RPS stops climbing as users increase).
2. **`steady`** – At the plateau user count, confirm P99 stays stable over time.
3. **`spike`** – Validate the system recovers (error rate drops, latency normalises)
   within 30–60 s of the spike ending.
4. **`stress`** – Push until errors exceed 5% to identify the hard ceiling.

### Common bottlenecks to watch

- **DB connection pool exhaustion** – sudden P95/P99 jump while P50 stays flat.
  Fix: raise `max_overflow` in SQLAlchemy config or add a replica.
- **Redis latency** – CSRF validation touches Redis on every mutating request;
  P50 for POST/PATCH/DELETE creeping up suggests Redis saturation.
- **Uvicorn worker saturation** – steady RPS ceiling despite more users.
  Fix: increase `--workers` count in the Docker uvicorn command.
