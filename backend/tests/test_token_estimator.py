"""Token-estimation tests."""
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import calculate_token_cost_micro_rub
from app.services.token_estimator import (
    _DEFAULT_OUTPUT_NO_HISTORY,
    _get_cached_float_settings,
    count_message_tokens,
    estimate_request_cost,
    estimate_request_cost_with_history,
)


# ── M5: batched settings cache reads ─────────────────────────────────────────


class _RecPipe:
    def __init__(self, vals):
        self.vals = vals
        self.ops = []
        self.executed = 0

    def get(self, key):
        self.ops.append(key)
        return self

    async def execute(self):
        self.executed += 1
        return self.vals


class _RecRedis:
    def __init__(self, vals):
        self._pipe = _RecPipe(vals)
        self.direct_gets = []

    def pipeline(self, transaction=False):
        return self._pipe

    async def get(self, key):
        self.direct_gets.append(key)
        return None

    async def setex(self, *a):
        return None


async def test_get_cached_float_settings_pipelines_cache_hits():
    # M5: both settings cached → ONE pipelined round-trip, no per-key direct gets,
    # values returned in spec order.
    redis = _RecRedis(["1.5", "2.0"])
    out = await _get_cached_float_settings(
        None, redis, [("estimation_buffer", 9.0), ("reservation_buffer_multiplier", 9.0)]
    )
    assert out == [1.5, 2.0]
    assert redis._pipe.executed == 1
    assert redis.direct_gets == []
    assert redis._pipe.ops == ["settings:estimation_buffer", "settings:reservation_buffer_multiplier"]

# Use a seeded model id (002_seed_model_pricing.py).
MODEL_ID = "openai/gpt-4.1-mini"


@pytest_asyncio.fixture(autouse=True)
async def _clear_settings_cache(redis_client: Redis):
    """Make every test start with cold settings cache so default values apply."""
    for key in ("settings:estimation_buffer", "settings:reservation_buffer_multiplier"):
        await redis_client.delete(key)
    yield
    for key in ("settings:estimation_buffer", "settings:reservation_buffer_multiplier"):
        await redis_client.delete(key)


# ── Pure function tests ──────────────────────────────────────────────────────


def test_count_tokens_english() -> None:
    n = count_message_tokens([{"role": "user", "content": "Hello"}])
    assert isinstance(n, int)
    assert n > 0


def test_russian_more_tokens_than_english() -> None:
    en = count_message_tokens(
        [{"role": "user", "content": "Tell me about the Russian winter please"}]
    )
    ru = count_message_tokens(
        [{"role": "user", "content": "Расскажи мне о русской зиме пожалуйста"}]
    )
    assert ru > en, f"Expected Russian to tokenize wider, got ru={ru} en={en}"


def test_costs_use_ceiling_division() -> None:
    # calculate_token_cost_micro_rub(1, 1) — 1 token at 1 μ₽/1M-tokens
    # should never round down to 0; ceiling guarantees ≥ 1 μ₽.
    assert calculate_token_cost_micro_rub(1, 1) == 1
    # Even fractional cost rounds up
    assert calculate_token_cost_micro_rub(1, 500_001) == 1
    assert calculate_token_cost_micro_rub(0, 999_999) == 0  # zero tokens → zero


# ── DB/Redis integration tests ───────────────────────────────────────────────


async def test_history_aware_no_history_uses_default_output(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """With no conversation history, output defaults to the conservative 500
    and input is just the (buffered) new-message estimate (FEATURE 2)."""
    est = await estimate_request_cost_with_history(
        db_session, redis_client, None, "Привет", MODEL_ID,
    )
    assert est.output_tokens == _DEFAULT_OUTPUT_NO_HISTORY
    assert est.input_tokens > 0
    assert est.estimated_cost_micro_rub > 0
    assert est.reservation_micro_rub >= est.estimated_cost_micro_rub


async def test_min_output_100_tokens(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    est = await estimate_request_cost(
        db_session,
        redis_client,
        messages=[{"role": "user", "content": "Hi"}],
        model_id=MODEL_ID,
    )
    assert est.output_tokens >= 100


async def test_output_capped_at_8192(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    # Construct a prompt large enough that input_tokens * 0.5 >> 8192.
    # ~20k tokens of plain text. With buffer 1.3 → ~26k input,
    # raw_output ≈ 13k → clamped to 8192.
    big = "word " * 20_000
    est = await estimate_request_cost(
        db_session,
        redis_client,
        messages=[{"role": "user", "content": big}],
        model_id=MODEL_ID,
    )
    assert est.output_tokens <= 8192
    assert est.output_tokens == 8192


async def test_reservation_approx_2x_cost(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    est = await estimate_request_cost(
        db_session,
        redis_client,
        messages=[{"role": "user", "content": "Write a short poem about Moscow."}],
        model_id=MODEL_ID,
    )
    assert est.estimated_cost_micro_rub > 0
    assert est.reservation_micro_rub >= est.estimated_cost_micro_rub
    # Default multiplier is 2.0 — reservation is exactly 2× the cost for integer math.
    assert est.reservation_micro_rub == est.estimated_cost_micro_rub * 2


async def test_estimation_buffer_applied(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    messages = [
        {"role": "user", "content": "Explain quantum entanglement in two sentences."},
    ]
    raw = count_message_tokens(messages)
    est = await estimate_request_cost(
        db_session, redis_client, messages=messages, model_id=MODEL_ID
    )
    # Default estimation_buffer is 1.3 → buffered input strictly exceeds raw
    # (truncation could in theory lose <1 token, but for non-trivial messages
    # raw × 1.3 > raw + 1).
    assert est.input_tokens > raw
    assert est.input_tokens == int(raw * 1.3)


async def test_estimate_uses_stored_rub_not_usd(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Estimation must read the stored RUB price columns, not recompute from USD.

    A free / override-priced model has openrouter_*_price_micro_usd == 0 but a
    non-zero input/output_price_micro_rub. The estimate (and therefore the
    reservation, which the DB requires to be > 0) must reflect the RUB columns
    so it matches what finalization actually charges.
    """
    from sqlalchemy import delete

    from app.models.model_pricing import ModelPricing

    test_model_id = "test/free-estimator-regression"
    row = ModelPricing(
        model_id=test_model_id,
        model_name="Free Estimator Regression",
        provider="test",
        openrouter_input_price_micro_usd=0,
        openrouter_output_price_micro_usd=0,
        input_price_micro_rub=10_000,
        output_price_micro_rub=10_000,
        is_price_override=True,
        max_context_tokens=8192,
        max_output_tokens=2048,
        status="available",
        is_current=True,
    )
    db_session.add(row)
    await db_session.commit()
    try:
        est = await estimate_request_cost(
            db_session,
            redis_client,
            messages=[{"role": "user", "content": "Hello there, how are you?"}],
            model_id=test_model_id,
        )
        # USD recompute would yield 0 → a 0 reservation (DB check violation).
        # Stored RUB columns are non-zero, so the estimate must be > 0.
        assert est.estimated_cost_micro_rub > 0
        assert est.reservation_micro_rub > 0
    finally:
        await db_session.execute(
            delete(ModelPricing).where(ModelPricing.model_id == test_model_id)
        )
        await db_session.commit()


async def test_estimate_uses_passed_pricing_not_requery(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """tr-vg4: when the caller passes the pricing row it already holds (pricing@STEP-2),
    estimation prices off THAT object — not a fresh re-query that could read a newer
    row mid model-sync. Proven by detaching the live row, bumping its in-memory price
    10×, and showing the passed-pricing estimate exceeds the re-query (DB) estimate."""
    from sqlalchemy import select

    from app.models.model_pricing import ModelPricing

    live = (
        await db_session.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == MODEL_ID, ModelPricing.is_current.is_(True)
            )
        )
    ).scalar_one()
    max_out = live.max_output_tokens

    # Re-query path (pricing=None) reads the DB row as-is.
    est_default = await estimate_request_cost_with_history(
        db_session, redis_client, None, "Привет", MODEL_ID, max_out
    )

    # Detach + bump prices 10× in memory; the DB row is untouched.
    db_session.expunge(live)
    live.input_price_micro_rub *= 10
    live.output_price_micro_rub *= 10
    est_passed = await estimate_request_cost_with_history(
        db_session, redis_client, None, "Привет", MODEL_ID, max_out, pricing=live
    )

    # Passed pricing (10×) is used → strictly larger than the re-query estimate.
    assert est_passed.estimated_cost_micro_rub > est_default.estimated_cost_micro_rub
