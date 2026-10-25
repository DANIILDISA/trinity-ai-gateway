"""Currency conversion primitives — SCHEMA.md §8.

Money inside Trinity is always stored as BIGINT micro-rubles (μ₽).
1 RUB = 1_000_000 μ₽. The same scale is used for micro-USD (1 USD = 1_000_000 μUSD).

The only float that enters this module is `usd_float` from OpenRouter; it is
converted to an int immediately and never stored or computed as a float again.

Rounding policy (SCHEMA.md §8.3): always in operator's favor.
  - Cost calculation:   ceil()  → user pays slightly more per request
  - Balance display:    floor() → user sees slightly less than actual
"""

MICRO_PER_UNIT: int = 1_000_000


def rubles_to_micro(rubles: int) -> int:
    """Whole rubles (user input, e.g., top-up amount) → micro-rubles."""
    return rubles * MICRO_PER_UNIT


def kopeks_to_micro(kopeks: int) -> int:
    """YooKassa kopeks → micro-rubles. 1 kopek = 10_000 μ₽."""
    return kopeks * 10_000


def micro_to_display_balance(micro: int) -> str:
    """Micro-rubles → balance display (2 decimal places, floor toward -∞).

    Floor semantics: 847.539999 RUB displays as "847.53", never "847.54".
    Negative inputs are floored toward -∞ (Python's // and % give a positive
    remainder), e.g. -500_000 μ₽ renders as "-1.50" (== -1 + 0.50 == -0.50 RUB).
    Production callers should not pass negatives — `balance_micro_rub >= 0`
    is enforced by the DB CHECK constraint.
    """
    rubles = micro // MICRO_PER_UNIT
    remainder = (micro % MICRO_PER_UNIT) // 10_000  # 2-decimal-place floor
    return f"{rubles}.{remainder:02d}"


def micro_to_api_string(micro: int) -> str:
    """Micro-rubles → API response string (4 decimal places, always, signed)."""
    sign = "-" if micro < 0 else ""
    abs_micro = abs(micro)
    rubles = abs_micro // MICRO_PER_UNIT
    frac = (abs_micro % MICRO_PER_UNIT) // 100  # 4-decimal-place floor
    return f"{sign}{rubles}.{frac:04d}"


def usd_float_to_micro_usd(usd_float: float) -> int:
    """OpenRouter USD float → micro-USD integer (banker's round to nearest).

    This is the ONLY entry point for floats into the financial path.
    The result is treated as an exact integer from here on.
    """
    return round(usd_float * MICRO_PER_UNIT)


def micro_usd_to_micro_rub(micro_usd: int, exchange_rate_micro_rub: int) -> int:
    """Micro-USD → micro-rubles using exchange rate. Ceiling division.

    `exchange_rate_micro_rub`: μ₽ per 1 USD. Example: 82_500_000 == 82.50 RUB/USD.
    Returned value is rounded UP — operator's favor.
    """
    return (
        micro_usd * exchange_rate_micro_rub + MICRO_PER_UNIT - 1
    ) // MICRO_PER_UNIT


def calculate_token_cost_micro_rub(
    tokens: int, price_per_million_micro_rub: int
) -> int:
    """Cost of N tokens at the given μ₽-per-1M-tokens rate. Ceiling division.

    Even one token at a non-zero price costs at least 1 μ₽ — rounded UP in
    operator's favor so partial-μ₽ remainders accumulate to Trinity, not users.
    """
    return (tokens * price_per_million_micro_rub + 999_999) // 1_000_000
