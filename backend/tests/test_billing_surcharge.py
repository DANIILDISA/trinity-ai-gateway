"""Unit tests for reservation surcharge pre-reservation (BUG: web-search flat
fee + extended-thinking reasoning budget were not pre-reserved, so the delta
between the token estimate and OpenRouter's cost-basis floor was silently
absorbed as overdraft at finalization).

`estimate_surcharge_micro_rub` is the μ₽ amount folded into the reservation
estimate on top of the token-based estimate.
"""
from app.core.constants import (
    calculate_token_cost_micro_rub,
    micro_usd_to_micro_rub,
    usd_float_to_micro_usd,
)
from app.models.model_pricing import ModelPricing
from app.services.billing_service import (
    WEB_SEARCH_SURCHARGE_USD,
    estimate_surcharge_micro_rub,
)

_RATE = 82_500_000  # 82.50 RUB/USD
_MARKUP = 1.25
_INPUT_PRICE = 1_000_000  # 1 µ₽ per token


def _pricing(*, supports_reasoning: bool, supports_web_search: bool) -> ModelPricing:
    return ModelPricing(
        model_id="x", model_name="x", provider="x",
        openrouter_input_price_micro_usd=0, openrouter_output_price_micro_usd=0,
        input_price_micro_rub=_INPUT_PRICE, output_price_micro_rub=3_000_000,
        max_context_tokens=8192, max_output_tokens=4096,
        status="available", is_current=True,
        supports_reasoning=supports_reasoning,
        supports_web_search=supports_web_search,
    )


def _expected_web_surcharge() -> int:
    cost_basis = micro_usd_to_micro_rub(
        usd_float_to_micro_usd(WEB_SEARCH_SURCHARGE_USD), _RATE
    )
    return (cost_basis * round(_MARKUP * 100) + 99) // 100


def test_no_surcharge_when_neither_enabled() -> None:
    pricing = _pricing(supports_reasoning=True, supports_web_search=True)
    assert estimate_surcharge_micro_rub(
        pricing,
        web_search_enabled=False,
        reasoning_budget=None,
        exchange_rate_micro_rub=_RATE,
        markup_multiplier=_MARKUP,
    ) == 0


def test_thinking_reserves_reasoning_budget_at_input_rate() -> None:
    """The reasoning budget (billed at the cheaper INPUT rate) is pre-reserved —
    the token estimate only covers a small visible-output window."""
    pricing = _pricing(supports_reasoning=True, supports_web_search=False)
    surcharge = estimate_surcharge_micro_rub(
        pricing,
        web_search_enabled=False,
        reasoning_budget=16384,
        exchange_rate_micro_rub=_RATE,
        markup_multiplier=_MARKUP,
    )
    assert surcharge == calculate_token_cost_micro_rub(16384, _INPUT_PRICE)
    assert surcharge > 0


def test_web_search_reserves_flat_fee_with_markup() -> None:
    """The web-search flat fee (USD, folded into usage.cost but not tokens) is
    converted to μ₽ with the same ceiling+markup math finalization uses."""
    pricing = _pricing(supports_reasoning=False, supports_web_search=True)
    surcharge = estimate_surcharge_micro_rub(
        pricing,
        web_search_enabled=True,
        reasoning_budget=None,
        exchange_rate_micro_rub=_RATE,
        markup_multiplier=_MARKUP,
    )
    assert surcharge == _expected_web_surcharge()
    assert surcharge > 0


def test_web_search_ignored_when_model_unsupported() -> None:
    """Mirrors the plugin gate: web-search surcharge only applies when the model
    advertises support (otherwise the flag is dropped and no search runs)."""
    pricing = _pricing(supports_reasoning=False, supports_web_search=False)
    assert estimate_surcharge_micro_rub(
        pricing,
        web_search_enabled=True,
        reasoning_budget=None,
        exchange_rate_micro_rub=_RATE,
        markup_multiplier=_MARKUP,
    ) == 0


def test_combined_thinking_and_web_search_sum() -> None:
    pricing = _pricing(supports_reasoning=True, supports_web_search=True)
    surcharge = estimate_surcharge_micro_rub(
        pricing,
        web_search_enabled=True,
        reasoning_budget=8192,
        exchange_rate_micro_rub=_RATE,
        markup_multiplier=_MARKUP,
    )
    expected = (
        calculate_token_cost_micro_rub(8192, _INPUT_PRICE)
        + _expected_web_surcharge()
    )
    assert surcharge == expected
