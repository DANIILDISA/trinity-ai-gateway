"""System monitoring helpers (Task 8.7)."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_engine
from app.models.alert import Alert
from app.models.api_key import ApiKey
from app.models.system_setting import SystemSetting
from app.tasks.health_check import BALANCE_KEY

LATENCY_STAGES: tuple[str, ...] = (
    "estimation",
    "reservation",
    "openrouter_ttfb",
    "openrouter_total",
    "finalization",
    "total",
)


# ── System (CPU / RAM / disk) ───────────────────────────────────────────────


def _read_proc_stat() -> tuple[int, int]:
    with open("/proc/stat", encoding="ascii") as fh:
        line = fh.readline()
    parts = line.split()
    nums = [int(p) for p in parts[1:]]
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    total = sum(nums)
    return total, idle


async def _cpu_percent(sample_seconds: float = 0.1) -> float:
    try:
        t1, i1 = _read_proc_stat()
        await asyncio.sleep(sample_seconds)
        t2, i2 = _read_proc_stat()
    except (OSError, ValueError):
        return 0.0
    dt = t2 - t1
    di = i2 - i1
    if dt <= 0:
        return 0.0
    return max(0.0, min(100.0, (dt - di) / dt * 100))


def _read_meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                v = v.strip()
                if v.endswith("kB"):
                    out[k] = int(v.split()[0])
    except (OSError, ValueError):
        return {}
    return out


def _ram_stats() -> tuple[float, float, float]:
    info = _read_meminfo()
    total_kb = info.get("MemTotal", 0)
    available_kb = info.get("MemAvailable", info.get("MemFree", 0))
    if total_kb <= 0:
        return 0.0, 0.0, 0.0
    used_kb = max(total_kb - available_kb, 0)
    return (used_kb / total_kb * 100, used_kb / 1024, total_kb / 1024)


def _disk_stats() -> tuple[float, float, float]:
    try:
        st = os.statvfs("/")
    except (OSError, AttributeError):
        return 0.0, 0.0, 0.0
    total = st.f_blocks * st.f_frsize
    free = st.f_bfree * st.f_frsize
    used = max(total - free, 0)
    if total <= 0:
        return 0.0, 0.0, 0.0
    return (used / total * 100, used / (1024**3), total / (1024**3))


async def get_system_stats() -> dict[str, Any]:
    cpu = await _cpu_percent()
    ram_pct, ram_used_mb, ram_total_mb = _ram_stats()
    disk_pct, disk_used_gb, disk_total_gb = _disk_stats()
    return {
        "cpu_percent": round(cpu, 1),
        "ram_percent": round(ram_pct, 1),
        "ram_used_mb": round(ram_used_mb, 1),
        "ram_total_mb": round(ram_total_mb, 1),
        "disk_percent": round(disk_pct, 1),
        "disk_used_gb": round(disk_used_gb, 2),
        "disk_total_gb": round(disk_total_gb, 2),
    }


# ── Database pool ───────────────────────────────────────────────────────────


def get_db_stats() -> dict[str, int]:
    try:
        engine = get_engine()
    except RuntimeError:
        return {"pool_size": 0, "checked_out": 0, "overflow": 0, "available": 0}
    pool = engine.sync_engine.pool
    try:
        pool_size = int(pool.size())  # type: ignore[union-attr]
    except AttributeError:
        pool_size = 0
    try:
        checked_out = int(pool.checkedout())  # type: ignore[union-attr]
    except AttributeError:
        checked_out = 0
    try:
        overflow = int(pool.overflow())  # type: ignore[union-attr]
    except AttributeError:
        overflow = 0
    return {
        "pool_size": pool_size,
        "checked_out": checked_out,
        "overflow": max(overflow, 0),
        "available": max(pool_size - checked_out, 0),
    }


# ── Redis ───────────────────────────────────────────────────────────────────


async def get_redis_stats(redis: Redis) -> dict[str, Any]:
    try:
        info = await redis.info()
    except Exception:  # noqa: BLE001
        return {
            "used_memory": "0",
            "max_memory": "0",
            "connected_clients": 0,
            "hit_rate_percent": 0.0,
            "total_commands": 0,
            "version": "?",
            "uptime_seconds": 0,
        }

    hits = int(info.get("keyspace_hits", 0) or 0)
    misses = int(info.get("keyspace_misses", 0) or 0)
    total = hits + misses
    hit_rate = (hits / total * 100) if total > 0 else 0.0

    return {
        "used_memory": str(
            info.get("used_memory_human") or info.get("used_memory") or "0"
        ),
        "max_memory": str(
            info.get("maxmemory_human") or info.get("maxmemory") or "0"
        ),
        "connected_clients": int(info.get("connected_clients", 0) or 0),
        "hit_rate_percent": round(hit_rate, 1),
        "total_commands": int(info.get("total_commands_processed", 0) or 0),
        "version": str(info.get("redis_version", "?")),
        "uptime_seconds": int(info.get("uptime_in_seconds", 0) or 0),
    }


# ── Latency percentiles ─────────────────────────────────────────────────────


_LATENCY_SAMPLE_TTL_SECONDS = 86_400  # rolling 24h window of samples per stage
_LATENCY_MAX_SAMPLES = 5_000  # cap per stage so the sorted set stays bounded


async def record_latency_samples(redis: Redis, timing: "Any") -> None:
    """Push per-stage latency (ms) into Redis sorted sets for the admin view.

    `timing` is a billing RequestTiming. Each stage becomes a member in
    `latency:{stage}` scored by its millisecond duration; the monitoring
    endpoint reads these to compute p50/p95/p99. Best-effort: any Redis error
    is swallowed so observability never breaks a billed request (FEATURE 6).
    """
    import uuid as _uuid

    try:
        # Batch all stages' zadd/zremrangebyrank/expire into ONE pipeline
        # round-trip (audit M6) instead of 3×N un-pipelined ops on the billed
        # request's hot path. transaction=False: independent best-effort writes,
        # no atomicity needed.
        pipe = redis.pipeline(transaction=False)
        wrote = False
        for stage in LATENCY_STAGES:
            value = timing.get(stage)
            if value is None:
                continue
            key = f"latency:{stage}"
            pipe.zadd(key, {_uuid.uuid4().hex: float(value)})
            # Trim oldest-by-insertion is impossible on a score-sorted set, so
            # cap by count: drop the lowest-latency entries when over the cap.
            # At MVP volume the cap is rarely hit; the TTL is the primary bound.
            pipe.zremrangebyrank(key, 0, -(_LATENCY_MAX_SAMPLES + 1))
            pipe.expire(key, _LATENCY_SAMPLE_TTL_SECONDS)
            wrote = True
        if wrote:
            await pipe.execute()
    except Exception:  # noqa: BLE001 — observability must never break billing
        return


def _percentile(sorted_values: list[float], p: float) -> float | None:
    if not sorted_values:
        return None
    if p <= 0:
        return sorted_values[0]
    if p >= 100:
        return sorted_values[-1]
    n = len(sorted_values)
    idx = max(0, min(n - 1, int((p / 100) * n)))
    return sorted_values[idx]


async def get_latency_percentiles(redis: Redis) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for stage in LATENCY_STAGES:
        key = f"latency:{stage}"
        try:
            members = await redis.zrange(key, 0, -1, withscores=True)
        except Exception:  # noqa: BLE001
            members = []
        scores = sorted(float(score) for _, score in members)
        if not scores:
            out[stage] = {
                "p50_ms": None,
                "p95_ms": None,
                "p99_ms": None,
                "sample_count": 0,
            }
            continue
        out[stage] = {
            "p50_ms": _percentile(scores, 50),
            "p95_ms": _percentile(scores, 95),
            "p99_ms": _percentile(scores, 99),
            "sample_count": len(scores),
        }
    return out


# ── OpenRouter status ───────────────────────────────────────────────────────


_BALANCE_LIVE_KEY = "openrouter:balance:live"
_BALANCE_LIVE_TTL_SECONDS = 60


async def get_openrouter_balance_live(db: AsyncSession, redis: Redis) -> float | None:
    """Real OpenRouter credit balance (USD), cached 60s (FEATURE 5).

    Calls GET /auth/key with the active primary key. Returns None — which the
    dashboard renders as "Недоступно" — when running on the demo proxy, when no
    active key exists, or when the API call fails. The 60s cache keeps page
    loads from hammering OpenRouter.
    """
    from app.config import settings
    from app.tasks.health_check import _fetch_openrouter_balance

    cached = await redis.get(_BALANCE_LIVE_KEY)
    if cached is not None:
        try:
            return float(cached)
        except (TypeError, ValueError):
            return None

    if settings.openrouter_demo_mode:
        # No real OpenRouter account behind the demo proxy.
        return None

    try:
        from app.services.openrouter_proxy import get_active_key

        api_key = await get_active_key(db)
        balance = await _fetch_openrouter_balance(api_key)
    except Exception:  # noqa: BLE001 — surface as "Недоступно"
        return None

    try:
        await redis.setex(_BALANCE_LIVE_KEY, _BALANCE_LIVE_TTL_SECONDS, f"{balance:.4f}")
    except RedisError:
        pass  # admin dashboard cache — fail open on an OOM cache write (tr-5rd7)
    return balance


# Window for treating a PaidModelsUnavailable 402 as "credits depleted" (tr-ttot):
# recent enough that paid models are currently failing, so a stale-positive manual
# override must not mask the real exhaustion.
_PAID_DEPLETION_WINDOW_MINUTES = 15


async def _balance_threshold_usd(db: AsyncSession) -> float | None:
    raw = (
        await db.execute(
            select(SystemSetting.value).where(
                SystemSetting.key == "openrouter_balance_threshold_usd"
            )
        )
    ).scalar_one_or_none()
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


async def _manual_balance_usd(db: AsyncSession) -> float | None:
    raw = (
        await db.execute(
            select(SystemSetting.value).where(
                SystemSetting.key == "openrouter_manual_balance_usd"
            )
        )
    ).scalar_one_or_none()
    if raw is None:
        return None
    try:
        return float(Decimal(str(raw)))
    except (InvalidOperation, TypeError, ValueError):
        return None


async def _paid_models_depleted(db: AsyncSession) -> bool:
    """True when a paid model returned 402 PaidModelsUnavailable recently — the
    real signal that platform credits are exhausted (the live balance probe is
    unreliable for provisioned keys). Read-only off the alert the OpenRouter error
    path already records (details.error_type)."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=_PAID_DEPLETION_WINDOW_MINUTES
    )
    count = (
        await db.execute(
            select(func.count())
            .select_from(Alert)
            .where(Alert.source == "openrouter")
            .where(Alert.details["error_type"].astext == "PaidModelsUnavailableError")
            .where(Alert.created_at >= cutoff)
        )
    ).scalar_one() or 0
    return count > 0


async def resolve_openrouter_balance(
    db: AsyncSession, redis: Redis
) -> tuple[float, bool, str]:
    """Single source of truth for the OpenRouter balance shown on BOTH /admin
    (dashboard) and /admin/monitoring (tr-ttot). Returns (balance_usd, warning,
    source) with source ∈ {"live", "manual", "depleted", "unavailable"}.

    OpenRouter's /auth/key reports 0/None for provisioned keys, so a 0 live value
    is ambiguous. Resolution order:
      • A recent PaidModelsUnavailable 402 ⇒ credits really exhausted: show the
        real (live or 0) value WITH a warning; the manual override must NOT mask
        it (acceptance: "ручной override не маскирует реальный 0 при живых 402").
      • else live > 0 ⇒ use the live value.
      • else fall back to the admin-entered manual override.
      • else 0, "unavailable" (warned — we cannot confirm any credits).
    """
    threshold = await _balance_threshold_usd(db)
    if await _paid_models_depleted(db):
        live = await get_openrouter_balance_live(db, redis)
        return (live or 0.0), True, "depleted"
    live = await get_openrouter_balance_live(db, redis)
    if live is not None and live > 0:
        return live, (threshold is not None and live < threshold), "live"
    manual = await _manual_balance_usd(db)
    if manual is not None:
        return manual, (threshold is not None and manual < threshold), "manual"
    return 0.0, True, "unavailable"


async def get_openrouter_status(
    db: AsyncSession, redis: Redis
) -> dict[str, Any]:
    # Single source of truth shared with the dashboard (tr-ttot): same value,
    # warning, and source on /admin and /admin/monitoring.
    balance, balance_warning, balance_source = await resolve_openrouter_balance(
        db, redis
    )

    active_key = (
        await db.execute(
            select(ApiKey.label, ApiKey.key_suffix)
            .where(ApiKey.is_primary.is_(True))
            .limit(1)
        )
    ).first()
    active_label = active_key[0] if active_key else None
    active_suffix = active_key[1] if active_key else None

    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    recent_errors = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Alert)
                .where(Alert.source == "openrouter")
                .where(Alert.severity.in_(("critical", "warning")))
                .where(Alert.created_at >= one_hour_ago)
            )
        ).scalar_one()
        or 0
    )

    return {
        "balance_usd": balance,
        "balance_warning": balance_warning,
        "balance_source": balance_source,
        "active_key_label": active_label,
        "active_key_suffix": active_suffix,
        "recent_error_count_1h": recent_errors,
    }
