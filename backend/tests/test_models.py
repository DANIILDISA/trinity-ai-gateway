"""Model pricing endpoint and tariff-service tests."""
import pytest_asyncio
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.model_pricing import ModelPricing
from app.services.tariff_service import (
    PRICING_CACHE_KEY,
    calculate_rub_price,
)

MODELS_URL = "/api/v1/models"

# Seed-migration model_ids (002_seed_model_pricing.py)
_SEEDED_IDS = [
    "anthropic/claude-sonnet-4-6",
    "anthropic/claude-haiku-4-5",
    "openai/gpt-4.1",
    "openai/gpt-4.1-mini",
]


@pytest_asyncio.fixture(autouse=True)
async def reset_pricing_state(db_session: AsyncSession, redis_client: Redis):
    """Always start tests with a clean cache and all seeded models 'available'."""
    await redis_client.delete(PRICING_CACHE_KEY)
    # Remove any test-only rows from prior runs.
    await db_session.execute(
        text("DELETE FROM model_pricing WHERE model_id LIKE 'test-cache/%'")
    )
    # Restore status on seeded models.
    await db_session.execute(
        text(
            "UPDATE model_pricing SET status='available' "
            "WHERE model_id = ANY(:ids)"
        ),
        {"ids": _SEEDED_IDS},
    )
    await db_session.commit()

    yield

    await redis_client.delete(PRICING_CACHE_KEY)
    await db_session.execute(
        text("DELETE FROM model_pricing WHERE model_id LIKE 'test-cache/%'")
    )
    await db_session.execute(
        text(
            "UPDATE model_pricing SET status='available' "
            "WHERE model_id = ANY(:ids)"
        ),
        {"ids": _SEEDED_IDS},
    )
    await db_session.commit()


# ── Pure function tests ──────────────────────────────────────────────────────


def test_calculate_rub_price_formula() -> None:
    # 3 USD per 1M tokens @ 85 RUB/USD, 1.15× markup → 293.25 RUB
    # base   = ceil(3_000_000 × 85_000_000 / 1_000_000) = 255_000_000
    # result = ceil(255_000_000 × 115 / 100)            = 293_250_000
    got = calculate_rub_price(3_000_000, 85_000_000, 1.15)
    assert got == 293_250_000

    # 0.40 USD @ 85 RUB/USD, 1.15× → 39.10 RUB
    # base   = ceil(400_000 × 85_000_000 / 1_000_000) = 34_000_000
    # result = ceil(34_000_000 × 115 / 100)            = 39_100_000
    got = calculate_rub_price(400_000, 85_000_000, 1.15)
    assert got == 39_100_000


def test_prices_use_ceiling_division() -> None:
    # 1 micro-USD × 85_000_000 rate / 1_000_000 = 85 exactly, then 1.15 markup
    # would float to 97.75 — ceiling forces it up to 98.
    got = calculate_rub_price(1, 85_000_000, 1.15)
    assert got == 98, f"Ceiling rounding broken: {got} != 98"

    # 1 micro-USD × 500_001 rate / 1_000_000 floats to 0.500001 → ceiling = 1.
    # Then 1 × 1.15 = 1.15 → ceiling = 2.
    got = calculate_rub_price(1, 500_001, 1.15)
    assert got == 2


# ── HTTP endpoint tests ──────────────────────────────────────────────────────


async def test_get_models_returns_seeded_active(http_client: AsyncClient) -> None:
    resp = await http_client.get(MODELS_URL)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "models" in data

    ids = {m["id"] for m in data["models"]}
    # The DB may hold additional models (e.g. seeded free models); assert the
    # migration-seeded set is present rather than that it's the only content.
    assert set(_SEEDED_IDS).issubset(ids)

    for m in data["models"]:
        # 4dp string formatting
        assert "." in m["input_price_per_m"]
        assert len(m["input_price_per_m"].split(".")[1]) == 4
        assert "." in m["output_price_per_m"]
        assert len(m["output_price_per_m"].split(".")[1]) == 4
        # Required fields
        assert m["name"]
        assert m["status"] == "available"
        assert isinstance(m["context_window"], int)
        assert m["last_updated"]


async def test_disabled_model_excluded(
    http_client: AsyncClient, db_session: AsyncSession, redis_client: Redis
) -> None:
    # Disable one model directly.
    await db_session.execute(
        text(
            "UPDATE model_pricing SET status='disabled' WHERE model_id = :mid"
        ),
        {"mid": "openai/gpt-4.1-mini"},
    )
    await db_session.commit()
    # Invalidate cache so the next read reflects the change.
    await redis_client.delete(PRICING_CACHE_KEY)

    resp = await http_client.get(MODELS_URL)
    assert resp.status_code == 200
    data = resp.json()
    ids = {m["id"] for m in data["models"]}
    # The disabled model drops out; the other seeded models remain present.
    assert "openai/gpt-4.1-mini" not in ids
    assert (set(_SEEDED_IDS) - {"openai/gpt-4.1-mini"}).issubset(ids)


async def test_pricing_cached_in_redis(
    http_client: AsyncClient, db_session: AsyncSession, redis_client: Redis
) -> None:
    # Cache is empty at fixture start.
    assert await redis_client.get(PRICING_CACHE_KEY) is None

    resp1 = await http_client.get(MODELS_URL)
    assert resp1.status_code == 200
    # Baseline count — the DB may hold seeded free models beyond the 4 seeds.
    baseline = len(resp1.json()["models"])

    # Cache key now exists with TTL ≈ 60s.
    cached_raw = await redis_client.get(PRICING_CACHE_KEY)
    assert cached_raw is not None
    ttl = await redis_client.ttl(PRICING_CACHE_KEY)
    assert 0 < ttl <= 60

    # Mutate DB without invalidating the cache: insert a new model.
    await db_session.execute(
        text(
            """
            INSERT INTO model_pricing (
                model_id, model_name, provider,
                openrouter_input_price_micro_usd, openrouter_output_price_micro_usd,
                input_price_micro_rub, output_price_micro_rub,
                max_context_tokens, status, is_current
            ) VALUES (
                'test-cache/probe', 'Cache Probe', 'Test',
                1000000, 2000000, 100000000, 200000000,
                100000, 'available', true
            )
            """
        )
    )
    await db_session.commit()

    # Second GET must still see the baseline (cache hit, not re-queried from DB).
    resp2 = await http_client.get(MODELS_URL)
    assert resp2.status_code == 200
    assert len(resp2.json()["models"]) == baseline
    ids = {m["id"] for m in resp2.json()["models"]}
    assert "test-cache/probe" not in ids

    # After cache invalidation, the new model appears (baseline + 1).
    await redis_client.delete(PRICING_CACHE_KEY)
    resp3 = await http_client.get(MODELS_URL)
    assert resp3.status_code == 200
    assert len(resp3.json()["models"]) == baseline + 1
    assert "test-cache/probe" in {m["id"] for m in resp3.json()["models"]}
