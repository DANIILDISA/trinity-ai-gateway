"""seed model_pricing with four launch models

Inserts pricing rows for Claude Sonnet 4.6, Claude Haiku 4.5, GPT-4.1, and
GPT-4.1 Mini. RUB prices are precomputed at the seed-time rate (85 RUB/USD)
and markup (1.15); they will be recalculated whenever the admin updates the
live exchange rate via the tariff service.

Revision ID: 002
Revises: 001
Create Date: 2026-05-16
"""
from __future__ import annotations

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


_SEED_RATE_MICRO_RUB = 85_000_000  # 85 RUB/USD
_SEED_MARKUP = 1.15
_MICRO = 1_000_000


def _rub_price(micro_usd: int) -> int:
    base = (micro_usd * _SEED_RATE_MICRO_RUB + _MICRO - 1) // _MICRO
    markup_int = round(_SEED_MARKUP * 100)
    return (base * markup_int + 99) // 100


_MODELS = [
    # (model_id, model_name, provider, input_usd, output_usd, max_context_tokens)
    ("anthropic/claude-sonnet-4-6", "Claude Sonnet 4.6", "Anthropic", 3.00, 15.00, 200_000),
    ("anthropic/claude-haiku-4-5",  "Claude Haiku 4.5",  "Anthropic", 0.80,  4.00, 200_000),
    ("openai/gpt-4.1",              "GPT-4.1",           "OpenAI",    2.00,  8.00, 128_000),
    ("openai/gpt-4.1-mini",         "GPT-4.1 Mini",      "OpenAI",    0.40,  1.60, 128_000),
]


def upgrade() -> None:
    for model_id, name, provider, in_usd, out_usd, ctx in _MODELS:
        in_micro_usd = int(in_usd * _MICRO)
        out_micro_usd = int(out_usd * _MICRO)
        in_rub = _rub_price(in_micro_usd)
        out_rub = _rub_price(out_micro_usd)
        op.execute(
            f"""
            INSERT INTO model_pricing (
                model_id, model_name, provider,
                openrouter_input_price_micro_usd, openrouter_output_price_micro_usd,
                input_price_micro_rub, output_price_micro_rub,
                max_context_tokens, status, is_current
            ) VALUES (
                '{model_id}', '{name}', '{provider}',
                {in_micro_usd}, {out_micro_usd},
                {in_rub}, {out_rub},
                {ctx}, 'available', true
            )
            """
        )


def downgrade() -> None:
    ids = ", ".join(f"'{m[0]}'" for m in _MODELS)
    op.execute(f"DELETE FROM model_pricing WHERE model_id IN ({ids})")
