"""Admin monitoring endpoints (Task 8.7).

GET /api/v1/admin/monitoring/system        — CPU/RAM/disk + DB pool + Redis
GET /api/v1/admin/monitoring/latency       — per-stage p50/p95/p99 from Redis
GET /api/v1/admin/monitoring/openrouter    — balance, key, recent errors
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_redis, require_admin
from app.models.user import User
from app.services.monitoring_service import (
    LATENCY_STAGES,
    get_db_stats,
    get_latency_percentiles,
    get_openrouter_status,
    get_redis_stats,
    get_system_stats,
)

router = APIRouter()


# ── Schemas ─────────────────────────────────────────────────────────────────


class SystemBlock(BaseModel):
    cpu_percent: float
    ram_percent: float
    ram_used_mb: float
    ram_total_mb: float
    disk_percent: float
    disk_used_gb: float
    disk_total_gb: float


class DatabaseBlock(BaseModel):
    pool_size: int
    checked_out: int
    overflow: int
    available: int


class RedisBlock(BaseModel):
    used_memory: str
    max_memory: str
    connected_clients: int
    hit_rate_percent: float
    total_commands: int
    version: str
    uptime_seconds: int


class SystemMonitoringResponse(BaseModel):
    system: SystemBlock
    database: DatabaseBlock
    redis: RedisBlock


class LatencyPercentiles(BaseModel):
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    sample_count: int


class LatencyResponse(BaseModel):
    stages: dict[str, LatencyPercentiles]


class OpenRouterStatusResponse(BaseModel):
    balance_usd: float | None
    balance_warning: bool
    # Same source label the dashboard exposes — single source of truth (tr-ttot).
    balance_source: Literal["live", "manual", "depleted", "unavailable"]
    active_key_label: str | None
    active_key_suffix: str | None
    recent_error_count_1h: int


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get("/system", response_model=SystemMonitoringResponse)
async def system_monitoring(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> SystemMonitoringResponse:
    sys_stats = await get_system_stats()
    db_stats = get_db_stats()
    redis_stats = await get_redis_stats(redis)
    # `db` is wired so the request keeps the require_admin DB session warm
    # for the audit / role-check path; no direct query here.
    _ = db
    return SystemMonitoringResponse(
        system=SystemBlock(**sys_stats),
        database=DatabaseBlock(**db_stats),
        redis=RedisBlock(**redis_stats),
    )


@router.get("/latency", response_model=LatencyResponse)
async def latency_monitoring(
    _: User = Depends(require_admin),
    redis: Redis = Depends(get_redis),
) -> LatencyResponse:
    raw = await get_latency_percentiles(redis)
    # Guarantee every known stage appears, even if get_latency_percentiles
    # somehow skipped one — keeps the frontend table stable.
    for stage in LATENCY_STAGES:
        raw.setdefault(
            stage,
            {"p50_ms": None, "p95_ms": None, "p99_ms": None, "sample_count": 0},
        )
    return LatencyResponse(
        stages={k: LatencyPercentiles(**v) for k, v in raw.items()},
    )


@router.get("/openrouter", response_model=OpenRouterStatusResponse)
async def openrouter_monitoring(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> OpenRouterStatusResponse:
    payload: dict[str, Any] = await get_openrouter_status(db, redis)
    return OpenRouterStatusResponse(**payload)
