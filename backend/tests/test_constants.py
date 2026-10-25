"""Tests for app.core.constants — currency conversion primitives.

Every test documents the invariant from SCHEMA.md §8 it verifies. Money is
safety-critical: a wrong rounding direction here compounds into real money
loss over millions of transactions.
"""
import pytest

from app.core.constants import (
    MICRO_PER_UNIT,
    calculate_token_cost_micro_rub,
    kopeks_to_micro,
    micro_to_api_string,
    micro_to_display_balance,
    micro_usd_to_micro_rub,
    rubles_to_micro,
    usd_float_to_micro_usd,
)


# --------------------------------------------------------------------------- #
# Constant
# --------------------------------------------------------------------------- #

def test_micro_per_unit_is_one_million():
    """Invariant (SCHEMA.md §1.1): 1 RUB = 1_000_000 μ₽."""
    assert MICRO_PER_UNIT == 1_000_000


# --------------------------------------------------------------------------- #
# rubles_to_micro
# --------------------------------------------------------------------------- #

def test_rubles_to_micro_zero():
    """Boundary: zero rubles is zero micro-rubles."""
    assert rubles_to_micro(0) == 0


def test_rubles_to_micro_one():
    """1 RUB = 1_000_000 μ₽ exactly."""
    assert rubles_to_micro(1) == 1_000_000


def test_rubles_to_micro_topup_amount():
    """Typical YooKassa top-up: 500 RUB = 500_000_000 μ₽."""
    assert rubles_to_micro(500) == 500_000_000


def test_rubles_to_micro_one_million_rubles():
    """Large value: 1_000_000 RUB. BIGINT (≤9.2e18 μ₽) easily fits."""
    assert rubles_to_micro(1_000_000) == 1_000_000_000_000


# --------------------------------------------------------------------------- #
# kopeks_to_micro  (YooKassa boundary)
# --------------------------------------------------------------------------- #

def test_kopeks_to_micro_zero():
    """Boundary: zero kopeks is zero micro-rubles."""
    assert kopeks_to_micro(0) == 0


def test_kopeks_to_micro_one_kopek():
    """SCHEMA.md §1.1: 1 kopek == 10_000 μ₽."""
    assert kopeks_to_micro(1) == 10_000


def test_kopeks_to_micro_one_ruble():
    """100 kopeks == 1 RUB == 1_000_000 μ₽."""
    assert kopeks_to_micro(100) == 1_000_000


def test_kopeks_to_micro_yookassa_typical():
    """YooKassa returns kopeks; 50_000 kopeks == 500 RUB == 500_000_000 μ₽."""
    assert kopeks_to_micro(50_000) == 500_000_000


def test_kopeks_and_rubles_agree():
    """Cross-check: 1 RUB via rubles_to_micro == 100 kopeks via kopeks_to_micro."""
    assert rubles_to_micro(1) == kopeks_to_micro(100)


# --------------------------------------------------------------------------- #
# micro_to_display_balance  (floor to 2 decimal places, operator's favor)
# --------------------------------------------------------------------------- #

def test_display_balance_zero():
    """Boundary: zero balance renders as "0.00"."""
    assert micro_to_display_balance(0) == "0.00"


def test_display_balance_floors_to_zero():
    """100 μ₽ (= 0.0001 RUB) floors to "0.00" — operator's favor at display."""
    assert micro_to_display_balance(100) == "0.00"


def test_display_balance_exact_ruble():
    """1_000_000 μ₽ = exactly 1 RUB."""
    assert micro_to_display_balance(1_000_000) == "1.00"


def test_display_balance_two_decimal_exact():
    """847.53 RUB exactly."""
    assert micro_to_display_balance(847_530_000) == "847.53"


def test_display_balance_floors_not_rounds():
    """847.534999 RUB FLOORS to "847.53", never rounds up to "847.54".

    Critical invariant (SCHEMA.md §8.3): display floor → operator keeps the
    sub-kopek remainder.
    """
    assert micro_to_display_balance(847_534_999) == "847.53"


def test_display_balance_just_under_one():
    """999_999 μ₽ = 0.999999 RUB floors to "0.99"."""
    assert micro_to_display_balance(999_999) == "0.99"


def test_display_balance_large():
    """Sanity: very large balance renders correctly."""
    assert micro_to_display_balance(1_234_567_890_123) == "1234567.89"


# --------------------------------------------------------------------------- #
# micro_to_api_string  (always 4dp, signed)
# --------------------------------------------------------------------------- #

def test_api_string_zero():
    """API contract: zero is always "0.0000" (4dp, no sign)."""
    assert micro_to_api_string(0) == "0.0000"


def test_api_string_positive_subruble():
    """360_000 μ₽ = 0.36 RUB → "0.3600" (trailing zeros kept)."""
    assert micro_to_api_string(360_000) == "0.3600"


def test_api_string_negative_subruble():
    """-360_000 μ₽ → "-0.3600" — sign preserved, magnitude unchanged."""
    assert micro_to_api_string(-360_000) == "-0.3600"


def test_api_string_one_ruble():
    """1_000_000 μ₽ → "1.0000" (always 4dp)."""
    assert micro_to_api_string(1_000_000) == "1.0000"


def test_api_string_negative_one_ruble():
    """-1_000_000 μ₽ → "-1.0000"."""
    assert micro_to_api_string(-1_000_000) == "-1.0000"


def test_api_string_sub_kopek_truncated():
    """Sub-kopek precision is truncated (floor), not rounded.

    360_099 μ₽ = 0.360099 RUB → "0.3600" (the trailing 99 μ₽ is dropped).
    """
    assert micro_to_api_string(360_099) == "0.3600"


# --------------------------------------------------------------------------- #
# usd_float_to_micro_usd  (the only float entry point)
# --------------------------------------------------------------------------- #

def test_usd_float_to_micro_usd_zero():
    """0.0 USD → 0 μUSD."""
    assert usd_float_to_micro_usd(0.0) == 0


def test_usd_float_to_micro_usd_one_dollar():
    """1.0 USD → 1_000_000 μUSD."""
    assert usd_float_to_micro_usd(1.0) == 1_000_000


def test_usd_float_to_micro_usd_openrouter_typical():
    """Acceptance: usd_float_to_micro_usd(0.003) == 3000."""
    assert usd_float_to_micro_usd(0.003) == 3000


def test_usd_float_to_micro_usd_rounds_to_nearest():
    """round-to-nearest (banker's), NOT floor. 0.0000005 USD → 0 or 1 by banker's."""
    # 1.5e-6 USD = 1.5 μUSD → rounds to 2 (banker's: half to even, 2 is even)
    assert usd_float_to_micro_usd(1.5e-6) == 2


# --------------------------------------------------------------------------- #
# micro_usd_to_micro_rub  (ceiling — operator's favor)
# --------------------------------------------------------------------------- #

def test_micro_usd_to_micro_rub_zero():
    """0 μUSD at any rate is 0 μ₽."""
    assert micro_usd_to_micro_rub(0, 82_500_000) == 0


def test_micro_usd_to_micro_rub_one_dollar_at_82_50():
    """1 USD (1_000_000 μUSD) at 82.50 RUB/USD == 82_500_000 μ₽ (= 82.50 RUB)."""
    assert micro_usd_to_micro_rub(1_000_000, 82_500_000) == 82_500_000


def test_micro_usd_to_micro_rub_three_dollars_at_82_50():
    """3 USD at 82.50 RUB/USD == 247_500_000 μ₽ (= 247.50 RUB).

    Exact math: 3_000_000 μUSD × 82_500_000 ÷ 1_000_000 == 247_500_000.
    """
    assert micro_usd_to_micro_rub(3_000_000, 82_500_000) == 247_500_000


def test_micro_usd_to_micro_rub_ceiling_at_minimum():
    """Smallest non-zero conversion still yields ≥ 1 μ₽ (operator's favor).

    1 μUSD at rate 1 μ₽/USD: exact = 1e-6 μ₽ → ceiling = 1 μ₽, not 0.
    """
    assert micro_usd_to_micro_rub(1, 1) == 1


def test_micro_usd_to_micro_rub_ceiling_not_floor():
    """Non-exact division ALWAYS rounds UP — never down.

    With rate 1 μ₽/USD, 1_500_001 μUSD: exact = 1.500001 μ₽ → ceil → 2.
    """
    assert micro_usd_to_micro_rub(1_500_001, 1) == 2


# --------------------------------------------------------------------------- #
# calculate_token_cost_micro_rub  (ceiling — operator's favor)
# --------------------------------------------------------------------------- #

def test_token_cost_zero_tokens():
    """0 tokens at any price is 0 μ₽."""
    assert calculate_token_cost_micro_rub(0, 320_000_000) == 0


def test_token_cost_acceptance_320():
    """Acceptance: calculate_token_cost_micro_rub(1, 320_000_000) == 320.

    1 token at 320 μ₽/token (price 320_000_000 μ₽ per 1M tokens).
    """
    assert calculate_token_cost_micro_rub(1, 320_000_000) == 320


def test_token_cost_smallest_charge_is_one_micro_rub():
    """1 token at 1 μ₽/M tokens: exact = 1e-6 μ₽ → ceil → 1 μ₽, never 0.

    Even the cheapest possible request must cost at least 1 μ₽.
    """
    assert calculate_token_cost_micro_rub(1, 1) == 1


def test_token_cost_just_under_million_tokens_rounds_up():
    """999_999 tokens at 1 μ₽/M: exact ≈ 0.999999 μ₽ → ceil → 1, not 0."""
    assert calculate_token_cost_micro_rub(999_999, 1) == 1


def test_token_cost_exactly_million_tokens():
    """1_000_000 tokens at 1 μ₽/M: exact == 1, ceil == 1."""
    assert calculate_token_cost_micro_rub(1_000_000, 1) == 1


def test_token_cost_just_over_million_tokens_rounds_up():
    """1_000_001 tokens at 1 μ₽/M: exact = 1.000001 → ceil → 2."""
    assert calculate_token_cost_micro_rub(1_000_001, 1) == 2


def test_token_cost_typical_request():
    """Sanity: 1000 tokens at 5_000_000 μ₽/M (= 5 μ₽/token) → 5000 μ₽ exactly."""
    assert calculate_token_cost_micro_rub(1000, 5_000_000) == 5_000


# --------------------------------------------------------------------------- #
# Cross-cutting: operator's favor under accumulation
# --------------------------------------------------------------------------- #

def test_ceiling_accumulates_in_operator_favor():
    """Sum of N ceiling-charges ≥ single charge for N×tokens.

    Real-world: per-request ceiling costs accumulate small μ₽ remainders
    in operator's favor — they NEVER under-charge.
    """
    price = 1  # μ₽ per 1M tokens — extreme case
    single = calculate_token_cost_micro_rub(10, price)  # 10 tokens at once
    split = sum(calculate_token_cost_micro_rub(1, price) for _ in range(10))
    assert split >= single


def test_no_float_in_return_types():
    """Every conversion that returns money returns int (never float).

    Type-level guarantee that no float survives past usd_float_to_micro_usd.
    """
    assert isinstance(rubles_to_micro(1), int)
    assert isinstance(kopeks_to_micro(1), int)
    assert isinstance(usd_float_to_micro_usd(0.5), int)
    assert isinstance(micro_usd_to_micro_rub(1, 1), int)
    assert isinstance(calculate_token_cost_micro_rub(1, 1), int)
