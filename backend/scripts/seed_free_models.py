#!/usr/bin/env python3
"""Fetch OpenRouter free models and upsert them into model_pricing.

Usage:
    docker compose exec backend python scripts/seed_free_models.py

Filters the OpenRouter catalogue to models where
``pricing.prompt == "0"`` AND ``pricing.completion == "0"`` and inserts any
that don't already have a current row.

Idempotent: a model whose model_id already has an is_current row is skipped.

Notes:
- The OpenRouter /models list is public; the API key is used as a bearer token
  when a primary, active key exists in the api_keys table, otherwise the call
  is made unauthenticated.
- Free models have an OpenRouter USD cost of 0, so the public price shown in the
  UI (recomputed from USD × rate × markup) renders as free. Billing, however,
  uses the stored RUB columns below — set to 10_000 μ₽ / 1M tokens
  (= 0.01 ₽ = 1 копейка / 1M tokens). is_price_override=True pins that rate so
  the exchange-rate updater can't recompute it down to 0 from the $0 USD price.
"""
from __future__ import annotations

import asyncio
import sys

import httpx
from redis.asyncio import Redis
from sqlalchemy import select

from app.config import settings
from app.core.database import dispose_engine, get_session_factory, init_engine
from app.core.security import decrypt_key
from app.models.api_key import ApiKey
from app.models.model_pricing import ModelPricing
from app.services.tariff_service import (
    PRICING_CACHE_KEY,
    PRICING_CURRENT_CACHE_KEY,
)

MODELS_URL = f"{settings.openrouter_base_url.rstrip('/')}/models"

# 10_000 μ₽ per 1M tokens == 0.01 ₽ == 1 копейка / 1M tokens.
FREE_MODEL_RUB_PRICE_PER_M = 10_000


def _provider_from_id(model_id: str) -> str:
    """OpenRouter ids look like "google/gemini-2.0-flash-exp:free"."""
    return model_id.split("/", 1)[0] if "/" in model_id else "openrouter"


async def _get_primary_key(db) -> str | None:
    row = (
        await db.execute(
            select(ApiKey).where(
                ApiKey.is_primary.is_(True), ApiKey.status == "active"
            )
        )
    ).scalar_one_or_none()
    return decrypt_key(row.encrypted_key) if row is not None else None


async def _fetch_free_models(api_key: str | None) -> list[dict]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(MODELS_URL, headers=headers)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    return [
        m
        for m in data
        if (m.get("pricing") or {}).get("prompt") == "0"
        and (m.get("pricing") or {}).get("completion") == "0"
    ]


async def main() -> None:
    init_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )
    inserted = 0
    skipped = 0
    no_context = 0
    try:
        session_factory = get_session_factory()
        async with session_factory() as db:
            api_key = await _get_primary_key(db)
            if api_key is None:
                print(
                    "⚠ No primary active key in api_keys — fetching the public "
                    "model list unauthenticated."
                )

            free_models = await _fetch_free_models(api_key)
            print(f"Found {len(free_models)} free models on OpenRouter.")

            for m in free_models:
                model_id = m["id"]
                existing = (
                    await db.execute(
                        select(ModelPricing).where(
                            ModelPricing.model_id == model_id,
                            ModelPricing.is_current.is_(True),
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    skipped += 1
                    continue

                top = m.get("top_provider") or {}
                max_context = m.get("context_length") or top.get("context_length")
                if not max_context:
                    # No usable context window — billing truncation needs it.
                    no_context += 1
                    print(f"  · skip (no context_length): {model_id}")
                    continue

                max_output = top.get("max_completion_tokens")

                db.add(
                    ModelPricing(
                        model_id=model_id[:128],
                        model_name=(m.get("name") or model_id)[:255],
                        provider=_provider_from_id(model_id)[:64],
                        openrouter_input_price_micro_usd=0,
                        openrouter_output_price_micro_usd=0,
                        input_price_micro_rub=FREE_MODEL_RUB_PRICE_PER_M,
                        output_price_micro_rub=FREE_MODEL_RUB_PRICE_PER_M,
                        is_price_override=True,
                        max_context_tokens=int(max_context),
                        max_output_tokens=int(max_output) if max_output else None,
                        status="available",
                        is_current=True,
                    )
                )
                inserted += 1
                print(f"  + insert: {model_id}")

            await db.commit()

        # Bust the pricing caches so GET /api/v1/models reflects the new rows
        # immediately rather than after the 60s TTL.
        redis = Redis.from_url(settings.redis_url)
        try:
            await redis.delete(PRICING_CACHE_KEY, PRICING_CURRENT_CACHE_KEY)
        finally:
            await redis.aclose()
    finally:
        await dispose_engine()

    print(
        f"=== Done: {inserted} inserted, {skipped} already existed"
        + (f", {no_context} skipped (no context)" if no_context else "")
        + " ==="
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
