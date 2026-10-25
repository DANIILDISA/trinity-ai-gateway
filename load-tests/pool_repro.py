"""tr-4fy.3 pool-exhaustion repro harness.

Runs against the backend (httpx is a core dependency, always installed — not a
prod extra). Logs in once via the CSRF double-submit flow, then fires `--waves`
bursts of `--concurrency` simultaneous GET /users/me (DB+Redis touch, no
backpressure limiter) and reports the HTTP status histogram (503s broken down by
error.code) plus latency percentiles.

CREDENTIALS: never hardcode. Pass --email/--password or set POOL_TEST_EMAIL /
POOL_TEST_PASSWORD in the environment.

Pass criterion for tr-4fy.3: `pool_timeout` count == 0. Any residual 503 must be
intended backpressure (`overloaded` / `request_timeout` / `io_timeout`).

Usage (inside container):
    export POOL_TEST_EMAIL=... POOL_TEST_PASSWORD=...
    python /loadtests/pool_repro.py --concurrency 400 --waves 6
"""
import argparse
import asyncio
import collections
import os
import time

import httpx


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


async def _login(client: httpx.AsyncClient, api: str, email: str, password: str) -> str:
    # 1) Bootstrap CSRF: any GET makes CSRFMiddleware Set-Cookie csrf_token.
    #    /health/live does zero DB/Redis I/O (never consumes a pool slot).
    await client.get(f"{api}/health/live")
    csrf = client.cookies.get("csrf_token")
    if not csrf:
        raise RuntimeError("no csrf_token cookie after GET /health/live")
    # 2) Login (mutating -> needs matching X-CSRF-Token header).
    r = await client.post(
        f"{api}/auth/login",
        json={"email": email, "password": password},
        headers={"X-CSRF-Token": csrf},
    )
    r.raise_for_status()
    token = r.json()["access_token"]
    if not token:
        raise RuntimeError("login returned empty access_token")
    return token


async def _probe(client: httpx.AsyncClient, url: str, auth: dict) -> tuple[int, str, float]:
    t0 = time.perf_counter()
    try:
        r = await client.get(url, headers=auth)
    except httpx.HTTPError as exc:
        return (0, type(exc).__name__, (time.perf_counter() - t0) * 1000)
    dt = (time.perf_counter() - t0) * 1000
    code = ""
    if r.status_code == 503:
        try:
            code = r.json().get("error", {}).get("code", "unknown")
        except Exception:
            code = "unparseable"
    return (r.status_code, code, dt)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://localhost:8000")
    p.add_argument("--api", default="/api/v1")
    p.add_argument("--email", default=os.environ.get("POOL_TEST_EMAIL"),
                   help="required (or set POOL_TEST_EMAIL)")
    p.add_argument("--password", default=os.environ.get("POOL_TEST_PASSWORD"),
                   help="required (or set POOL_TEST_PASSWORD)")
    p.add_argument("--endpoint", default="/users/me")
    p.add_argument("--concurrency", type=int, default=400)
    p.add_argument("--waves", type=int, default=6)
    args = p.parse_args()
    if not args.email or not args.password:
        p.error("--email and --password are required (or set POOL_TEST_EMAIL / POOL_TEST_PASSWORD)")

    api = f"{args.base}{args.api}"
    # max_keepalive_connections=0 forces a fresh socket per request (correct for a
    # saturation repro). The p50/p95/p99 output therefore includes per-request TCP
    # setup — it's diagnostic, not a precision pool-latency benchmark. The pass gate
    # is the pool_timeout COUNT, not any latency threshold.
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=0)
    timeout = httpx.Timeout(35.0)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        token = await _login(client, api, args.email, args.password)
        auth = {"Authorization": f"Bearer {token}"}
        url = f"{api}{args.endpoint}"

        status_hist: collections.Counter = collections.Counter()
        code_hist: collections.Counter = collections.Counter()
        all_latencies: list[float] = []
        pool_timeout_latencies: list[float] = []
        for wave in range(args.waves):
            results = await asyncio.gather(
                *(_probe(client, url, auth) for _ in range(args.concurrency))
            )
            for status, code, dt in results:
                status_hist[status] += 1
                all_latencies.append(dt)
                if status == 503:
                    code_hist[code] += 1
                    if code == "pool_timeout":
                        pool_timeout_latencies.append(dt)
            print(f"wave {wave + 1}/{args.waves}: {dict(status_hist)}", flush=True)

    total = sum(status_hist.values())
    print("\n===== POOL REPRO RESULT =====")
    print(f"total requests : {total}")
    print(f"status hist     : {dict(status_hist)}")
    print(f"503 code hist   : {dict(code_hist)}")
    print(f"POOL_TIMEOUT_503: {code_hist.get('pool_timeout', 0)}")
    print(f"latency ms all  : p50={_pct(all_latencies,50):.1f} "
          f"p95={_pct(all_latencies,95):.1f} p99={_pct(all_latencies,99):.1f}")
    if pool_timeout_latencies:
        print(f"latency ms pool_timeout: p50={_pct(pool_timeout_latencies,50):.1f} "
              f"p95={_pct(pool_timeout_latencies,95):.1f}")


if __name__ == "__main__":
    asyncio.run(main())
