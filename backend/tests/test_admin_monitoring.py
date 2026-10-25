"""Tests for the admin monitoring endpoints (Task 8.7)."""
from __future__ import annotations

import uuid as _uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AdminAuditLog
from app.models.system_setting import SystemSetting
from app.models.transaction import Transaction
from app.models.user import User
from app.services.alert_service import create_alert
from app.services.monitoring_service import (
    _BALANCE_LIVE_KEY,
    record_latency_samples,
    resolve_openrouter_balance,
)
from app.tasks.health_check import BALANCE_KEY


# ── record_latency_samples batching (audit M6) ───────────────────────────────


class _RecordingPipeline:
    """redis-py asyncio pipeline shape: commands buffer SYNCHRONOUSLY (return
    self for chaining); only execute() is awaited."""

    def __init__(self) -> None:
        self.ops: list[str] = []
        self.executed = 0

    def zadd(self, *a, **k):
        self.ops.append("zadd")
        return self

    def zremrangebyrank(self, *a, **k):
        self.ops.append("zrem")
        return self

    def expire(self, *a, **k):
        self.ops.append("expire")
        return self

    async def execute(self):
        self.executed += 1
        return []


class _RecordingRedis:
    def __init__(self) -> None:
        self.pipe = _RecordingPipeline()
        self.direct: list[str] = []

    def pipeline(self, transaction: bool = False):
        return self.pipe

    async def zadd(self, *a, **k):
        self.direct.append("zadd")

    async def zremrangebyrank(self, *a, **k):
        self.direct.append("zrem")

    async def expire(self, *a, **k):
        self.direct.append("expire")


class _FakeTiming:
    def __init__(self, values: dict[str, float]) -> None:
        self._values = values

    def get(self, stage: str):
        return self._values.get(stage)


@pytest.mark.asyncio
async def test_record_latency_samples_batches_into_one_pipeline() -> None:
    # M6: 3 Redis ops/stage (zadd+zrem+expire) were issued un-pipelined — one
    # round-trip each. They must batch into a SINGLE pipeline.execute().
    redis = _RecordingRedis()
    timing = _FakeTiming({"estimation": 5.0, "total": 12.0})  # 2 stages present

    await record_latency_samples(redis, timing)

    assert redis.direct == []  # nothing issued directly on the client
    assert redis.pipe.executed == 1  # one batched round-trip
    assert redis.pipe.ops == ["zadd", "zrem", "expire", "zadd", "zrem", "expire"]


@pytest.mark.asyncio
async def test_record_latency_samples_no_pipeline_when_nothing_to_record() -> None:
    redis = _RecordingRedis()
    await record_latency_samples(redis, _FakeTiming({}))
    assert redis.pipe.executed == 0  # empty timing → no round-trip at all

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
SYSTEM_URL = "/api/v1/admin/monitoring/system"
LATENCY_URL = "/api/v1/admin/monitoring/latency"
OPENROUTER_URL = "/api/v1/admin/monitoring/openrouter"

_TEST_EMAILS = ["t87_admin@example.com"]
_LATENCY_KEYS = (
    "latency:estimation",
    "latency:reservation",
    "latency:openrouter_ttfb",
    "latency:openrouter_total",
    "latency:finalization",
    "latency:total",
)


@pytest_asyncio.fixture(autouse=True)
async def clean_state(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    for email in _TEST_EMAILS:
        existing = (
            await db_session.execute(
                select(User).where(User.email_lower == email.lower())
            )
        ).scalar_one_or_none()
        if existing is not None:
            await db_session.execute(
                sa_delete(Transaction).where(
                    Transaction.admin_user_id == existing.id
                )
            )
            await db_session.execute(
                sa_delete(AdminAuditLog).where(
                    AdminAuditLog.actor_id == existing.id
                )
            )
            await db_session.delete(existing)
    await db_session.commit()

    # Wipe latency keys + openrouter balance from prior runs.
    await redis_client.delete(*_LATENCY_KEYS)
    await redis_client.delete(BALANCE_KEY)


async def _register_admin(
    http: AsyncClient, db: AsyncSession, email: str
) -> str:
    r = await http.post(
        REGISTER_URL,
        json={"email": email, "password": "SecurePass1", "display_name": "Имя"},
    )
    assert r.status_code == 200, r.text
    uid = r.json()["user"]["id"]
    user = (
        await db.execute(select(User).where(User.id == _uuid.UUID(uid)))
    ).scalar_one()
    user.role = "admin"
    await db.commit()
    login = await http.post(
        LOGIN_URL, json={"email": email, "password": "SecurePass1"}
    )
    assert login.status_code == 200
    return login.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_system_stats(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _register_admin(http_client, db_session, "t87_admin@example.com")
    resp = await http_client.get(SYSTEM_URL, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    sysblock = data["system"]
    for k in ("cpu_percent", "ram_percent", "disk_percent"):
        assert 0.0 <= sysblock[k] <= 100.0, f"{k}={sysblock[k]} out of range"
    assert sysblock["ram_total_mb"] >= 0.0
    assert sysblock["disk_total_gb"] >= 0.0

    dbblock = data["database"]
    assert dbblock["pool_size"] >= 1
    assert dbblock["available"] >= 0


async def test_redis_stats(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _register_admin(http_client, db_session, "t87_admin@example.com")
    resp = await http_client.get(SYSTEM_URL, headers=_auth(token))
    assert resp.status_code == 200
    redis_block = resp.json()["redis"]
    assert redis_block["version"] not in ("", "?")
    assert redis_block["connected_clients"] >= 1
    assert redis_block["uptime_seconds"] >= 0


async def test_latency_empty(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _register_admin(http_client, db_session, "t87_admin@example.com")
    resp = await http_client.get(LATENCY_URL, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    stages = resp.json()["stages"]
    # All 6 stages must be present, all with sample_count=0.
    for stage in (
        "estimation",
        "reservation",
        "openrouter_ttfb",
        "openrouter_total",
        "finalization",
        "total",
    ):
        assert stage in stages, f"missing stage {stage}"
        assert stages[stage]["sample_count"] == 0
        assert stages[stage]["p50_ms"] is None
        assert stages[stage]["p95_ms"] is None
        assert stages[stage]["p99_ms"] is None


async def test_latency_with_data(
    http_client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
) -> None:
    token = await _register_admin(http_client, db_session, "t87_admin@example.com")
    # Seed 3 values; p50 should be 200.
    await redis_client.zadd(
        "latency:total", {"sample-a": 100, "sample-b": 200, "sample-c": 300}
    )

    resp = await http_client.get(LATENCY_URL, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    total = resp.json()["stages"]["total"]
    assert total["sample_count"] == 3
    # Nearest-rank: idx = floor(0.5 × 3) = 1 → sorted[1] = 200.
    assert total["p50_ms"] is not None
    assert abs(total["p50_ms"] - 200) <= 1
    # p95 / p99 fall on the last element with 3 samples.
    assert abs(total["p95_ms"] - 300) <= 1
    assert abs(total["p99_ms"] - 300) <= 1


async def test_openrouter_status(
    http_client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
) -> None:
    token = await _register_admin(http_client, db_session, "t87_admin@example.com")
    # Monitoring resolves via the shared resolver (tr-ttot) → the live cache key.
    await redis_client.set(_BALANCE_LIVE_KEY, "42.50")
    await _set_setting(db_session, "openrouter_balance_threshold_usd", "100.0")

    resp = await http_client.get(OPENROUTER_URL, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["balance_usd"] is not None
    assert abs(data["balance_usd"] - 42.50) < 1e-6
    # 42.50 < 100 → warning; live value used directly.
    assert data["balance_warning"] is True
    assert data["balance_source"] == "live"
    assert "recent_error_count_1h" in data


# ── Shared OpenRouter-balance resolver (tr-ttot: single source of truth) ──────


async def _set_setting(db: AsyncSession, key: str, value: str) -> None:
    existing = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    ).scalar_one_or_none()
    if existing is not None:
        existing.value = value
    else:
        db.add(SystemSetting(key=key, value=value))
    await db.commit()


async def test_resolve_balance_manual_fallback_when_live_zero(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """tr-ttot: the live probe is 0 (unreliable for provisioned keys) and no paid
    model is 402'ing → fall back to the admin manual override, source 'manual'."""
    await redis_client.set(_BALANCE_LIVE_KEY, "0.0")
    await _set_setting(db_session, "openrouter_manual_balance_usd", "11.0")
    await _set_setting(db_session, "openrouter_balance_threshold_usd", "10.0")

    balance, warning, source = await resolve_openrouter_balance(
        db_session, redis_client
    )
    assert source == "manual"
    assert abs(balance - 11.0) < 1e-6
    assert warning is False  # 11 > 10


async def test_resolve_balance_depleted_402_does_not_let_manual_mask(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """tr-ttot acceptance: a recent PaidModelsUnavailable 402 means credits are
    really depleted — the manual override must NOT mask it; show real low + warn."""
    await redis_client.set(_BALANCE_LIVE_KEY, "0.0")
    await _set_setting(db_session, "openrouter_manual_balance_usd", "11.0")
    await _set_setting(db_session, "openrouter_balance_threshold_usd", "10.0")
    await create_alert(
        db_session,
        severity="warning",
        source="openrouter",
        title="OpenRouter request failed",
        message="Request x failed: PaidModelsUnavailableError",
        details={"error_type": "PaidModelsUnavailableError"},
    )
    await db_session.commit()

    balance, warning, source = await resolve_openrouter_balance(
        db_session, redis_client
    )
    assert source == "depleted"
    assert warning is True
    assert balance < 10.0  # the 11.0 manual did NOT mask the real depletion


async def test_resolve_balance_live_positive_wins(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """A real positive live balance is used directly, source 'live'."""
    await redis_client.set(_BALANCE_LIVE_KEY, "42.50")
    await _set_setting(db_session, "openrouter_manual_balance_usd", "11.0")
    await _set_setting(db_session, "openrouter_balance_threshold_usd", "10.0")

    balance, warning, source = await resolve_openrouter_balance(
        db_session, redis_client
    )
    assert source == "live"
    assert abs(balance - 42.50) < 1e-6
    assert warning is False  # 42.50 > 10


async def test_dashboard_and_monitoring_agree_on_balance(
    http_client: AsyncClient, db_session: AsyncSession, redis_client: Redis
) -> None:
    """tr-ttot / tr-aw9e: /admin (dashboard) and /admin/monitoring show the SAME
    OpenRouter balance, warning, and source — single source of truth."""
    token = await _register_admin(http_client, db_session, "t87_admin@example.com")
    await redis_client.set(_BALANCE_LIVE_KEY, "0.0")
    await _set_setting(db_session, "openrouter_manual_balance_usd", "11.0")
    await _set_setting(db_session, "openrouter_balance_threshold_usd", "10.0")

    dash = (
        await http_client.get("/api/v1/admin/dashboard", headers=_auth(token))
    ).json()
    mon = (
        await http_client.get(OPENROUTER_URL, headers=_auth(token))
    ).json()

    assert abs(dash["openrouter_balance_usd"] - mon["balance_usd"]) < 1e-6
    assert dash["openrouter_balance_source"] == mon["balance_source"] == "manual"
    assert dash["openrouter_balance_warning"] is False
    assert mon["balance_warning"] is False
