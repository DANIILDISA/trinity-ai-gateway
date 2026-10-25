"""Tests for the OpenRouter health probe (Task 6.4)."""
import pytest
import pytest_asyncio
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import delete as sa_delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import encrypt_key
from app.models.alert import Alert
from app.models.api_key import ApiKey
from app.models.system_setting import SystemSetting
from app.tasks.health_check import (
    BALANCE_KEY,
    MODEL_HEALTH_KEY_PREFIX,
    check_openrouter_health,
)

MODELS_URL = "/api/v1/models"

_SEEDED_IDS = [
    "anthropic/claude-sonnet-4-6",
    "anthropic/claude-haiku-4-5",
    "openai/gpt-4.1",
    "openai/gpt-4.1-mini",
]


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _force_real_probe(monkeypatch: pytest.MonkeyPatch):
    """Disable DEMO_MODE for this file — these tests exercise the real
    OpenRouter probe path, which is bypassed when settings.demo_mode is True.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "demo_mode", False)


@pytest_asyncio.fixture(autouse=True)
async def _isolate_state(db_session: AsyncSession, redis_client: Redis):
    """Wipe leftover api_keys + openrouter alerts + Redis health keys."""
    await db_session.execute(sa_delete(ApiKey))
    await db_session.execute(
        sa_delete(Alert).where(Alert.source == "openrouter")
    )
    await db_session.commit()

    keys = await redis_client.keys(f"{MODEL_HEALTH_KEY_PREFIX}*")
    if keys:
        await redis_client.delete(*keys)
    await redis_client.delete(BALANCE_KEY)
    # Reset model statuses to 'available' so the health checker iterates them all.
    await db_session.execute(
        text("UPDATE model_pricing SET status='available' WHERE model_id = ANY(:ids)"),
        {"ids": _SEEDED_IDS},
    )
    await db_session.commit()

    yield

    await db_session.execute(sa_delete(ApiKey))
    await db_session.execute(
        sa_delete(Alert).where(Alert.source == "openrouter")
    )
    await db_session.commit()
    keys = await redis_client.keys(f"{MODEL_HEALTH_KEY_PREFIX}*")
    if keys:
        await redis_client.delete(*keys)
    await redis_client.delete(BALANCE_KEY)


async def _seed_mock_api_key(db: AsyncSession, value: str = "mock-test") -> None:
    db.add(ApiKey(
        label="health-test",
        encrypted_key=encrypt_key(value),
        key_suffix=value[-8:],
        status="active",
        is_primary=True,
    ))
    await db.commit()


async def _set_threshold(db: AsyncSession, threshold_usd: float) -> None:
    """Update the JSONB system_settings row via ORM to avoid `::jsonb` cast
    syntax collisions with SQLAlchemy's bind-parameter parser."""
    row = (
        await db.execute(
            select(SystemSetting).where(
                SystemSetting.key == "openrouter_balance_threshold_usd"
            )
        )
    ).scalar_one()
    row.value = threshold_usd
    await db.commit()


# ── Tests ───────────────────────────────────────────────────────────────────


async def test_health_check_stores_balance_in_redis(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    await _seed_mock_api_key(db_session)
    await _set_threshold(db_session, 100.0)  # mock balance 500 > 100, no alert

    result = await check_openrouter_health(db_session, redis_client)
    assert result["error"] is None
    assert result["balance_usd"] == 500.0

    cached = await redis_client.get(BALANCE_KEY)
    assert cached is not None
    assert float(cached) == 500.0
    ttl = await redis_client.ttl(BALANCE_KEY)
    assert 0 < ttl <= 360


async def test_health_check_marks_models_available(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    await _seed_mock_api_key(db_session)
    await _set_threshold(db_session, 100.0)

    await check_openrouter_health(db_session, redis_client)

    for model_id in _SEEDED_IDS:
        value = await redis_client.get(f"{MODEL_HEALTH_KEY_PREFIX}{model_id}")
        assert value == "available", f"{model_id}: expected 'available', got {value!r}"
        ttl = await redis_client.ttl(f"{MODEL_HEALTH_KEY_PREFIX}{model_id}")
        assert 0 < ttl <= 360


async def test_health_check_creates_alert_on_low_balance(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    await _seed_mock_api_key(db_session)
    # Threshold above mock balance of 500 → alert must fire.
    await _set_threshold(db_session, 999_999.0)

    await check_openrouter_health(db_session, redis_client)

    db_session.expire_all()
    alerts = (
        await db_session.execute(
            select(Alert).where(
                Alert.source == "openrouter",
                Alert.severity == "critical",
            )
        )
    ).scalars().all()
    assert len(alerts) == 1
    alert = alerts[0]
    assert "balance low" in alert.title.lower()
    assert alert.details["balance_usd"] == 500.0
    assert alert.details["threshold_usd"] == 999_999.0


async def test_health_check_marks_all_unavailable_when_no_active_key(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    # No seeded key — get_active_key raises 503; checker must mark every
    # current model as unavailable and not write the balance key.
    result = await check_openrouter_health(db_session, redis_client)
    assert result["error"] == "no_active_key"
    assert result["balance_usd"] is None
    assert await redis_client.get(BALANCE_KEY) is None
    for model_id in _SEEDED_IDS:
        value = await redis_client.get(f"{MODEL_HEALTH_KEY_PREFIX}{model_id}")
        assert value == "unavailable"


async def test_models_endpoint_reflects_redis_status(
    http_client: AsyncClient, redis_client: Redis
) -> None:
    # Force one specific model to "unavailable" in Redis directly.
    target = "openai/gpt-4.1-mini"
    await redis_client.setex(f"{MODEL_HEALTH_KEY_PREFIX}{target}", 360, "unavailable")
    # Bust the pricing cache so list_models re-runs (and applies Redis overlay).
    from app.services.tariff_service import PRICING_CACHE_KEY
    await redis_client.delete(PRICING_CACHE_KEY)

    resp = await http_client.get(MODELS_URL)
    assert resp.status_code == 200
    data = resp.json()
    by_id = {m["id"]: m for m in data["models"]}
    assert by_id[target]["status"] == "unavailable"
    # The others retain their DB-default status ('available').
    for other_id in _SEEDED_IDS:
        if other_id == target:
            continue
        assert by_id[other_id]["status"] == "available"


async def test_models_endpoint_available_when_no_redis_key(
    http_client: AsyncClient, redis_client: Redis
) -> None:
    # Make sure no model_health:* keys exist.
    keys = await redis_client.keys(f"{MODEL_HEALTH_KEY_PREFIX}*")
    if keys:
        await redis_client.delete(*keys)
    from app.services.tariff_service import PRICING_CACHE_KEY
    await redis_client.delete(PRICING_CACHE_KEY)

    resp = await http_client.get(MODELS_URL)
    assert resp.status_code == 200
    data = resp.json()
    by_id = {m["id"]: m for m in data["models"]}
    # No crash, no KeyError — seeded models surface with their DB status
    # (available). The DB may hold additional seeded free models, so assert the
    # seeded set is present rather than that it's the only content.
    assert set(_SEEDED_IDS).issubset(by_id)
    assert all(by_id[mid]["status"] == "available" for mid in _SEEDED_IDS)
