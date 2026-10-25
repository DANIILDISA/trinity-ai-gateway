"""Automatic OpenRouter model catalogue sync.

Fetches the live model list from OpenRouter, filters out free models, converts
USD-per-token prices into Trinity's μ₽-per-1M-tokens scale (reusing the same
arithmetic as the tariff service), and upserts the `model_pricing` table.

Models that vanish from the upstream response are flagged status='unavailable'
rather than deleted, so their pricing history and any in-flight references
survive. Billing logic is never touched — only `model_pricing` is written.

All price arithmetic uses Decimal until the final integer μ-unit; no float
ever enters the financial path here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.model_pricing import ModelPricing
from app.services.openrouter_proxy import get_active_key
from app.services.tariff_service import (
    PRICING_CACHE_KEY,
    PRICING_CURRENT_CACHE_KEY,
    _get_pricing_settings,
    calculate_rub_price,
)

logger = logging.getLogger(__name__)

# USD-per-token → μUSD-per-1M-tokens: ×1e6 (tokens per million) ×1e6 (micro).
_USD_PER_TOKEN_TO_MICRO_USD_PER_MILLION = Decimal(10) ** 12
_MODELS_TIMEOUT_SECONDS = 30.0

# Per-chunk stream timeouts (seconds). Reasoning / deep-research models can fall
# silent for minutes between chunks, so they get a far longer ceiling.
_DEFAULT_STREAM_TIMEOUT_SECONDS = 30
_REASONING_STREAM_TIMEOUT_SECONDS = 300
_REASONING_MODEL_MARKERS = (
    "deepresearch",
    "deep-research",
    "r1",
    "thinking",
    "reasoner",
    "o1",
    "o3",
)


def _stream_timeout_for(model_id: str) -> int:
    """Per-chunk stream timeout for a model, inferred from its id.

    Reasoning / deep-research families think for minutes between chunks, so they
    get 300s; everything else keeps the 30s default.
    """
    lowered = model_id.lower()
    if any(marker in lowered for marker in _REASONING_MODEL_MARKERS):
        return _REASONING_STREAM_TIMEOUT_SECONDS
    return _DEFAULT_STREAM_TIMEOUT_SECONDS


@dataclass
class SyncResult:
    added: int = 0
    updated: int = 0
    deactivated: int = 0
    errors: list[str] = field(default_factory=list)


def _to_micro_usd_per_million(usd_per_token: Any) -> int:
    """Convert an OpenRouter USD-per-token price string to μUSD per 1M tokens.

    Uses Decimal throughout; the result is the integer scale stored in
    `model_pricing.openrouter_*_price_micro_usd` (e.g. $3/1M → 3_000_000).
    """
    value = Decimal(str(usd_per_token))
    micro = value * _USD_PER_TOKEN_TO_MICRO_USD_PER_MILLION
    # Quantize to whole μUSD; prices are non-negative so rounding down is safe.
    return int(micro.to_integral_value(rounding="ROUND_HALF_UP"))


def _provider_from_model_id(model_id: str) -> str:
    """OpenRouter ids are `provider/model`; fall back to the id when unslashed."""
    prefix = model_id.split("/", 1)[0] if "/" in model_id else model_id
    return prefix[:1].upper() + prefix[1:] if prefix else model_id


def _has_real_pricing(pricing: dict[str, Any]) -> bool:
    """True only when both prompt and completion are fixed, positive prices.

    Dynamic-price routers (e.g. "Auto Router") report prompt as None/-1/negative,
    and broken or free-tier entries report 0; both must stay out of the catalogue.
    Only models with prompt > 0 AND completion > 0 are synced.
    """
    try:
        prompt = Decimal(str(pricing.get("prompt")))
        completion = Decimal(str(pricing.get("completion")))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return prompt > 0 and completion > 0


# Models/providers that advertise `tools` in OpenRouter's `supported_parameters`
# but do NOT reliably perform OpenAI-style function-calling through OpenRouter:
# they ignore the tools and answer in prose, so the agentic loop sees zero
# tool_calls and the model hallucinates having created a file (tr-1kb, live with
# AI21 Jamba). Force supports_tools=False for these so the «Инструменты» toggle is
# hidden and a user is never promised a file that was never created. Because the
# override lives in the sync derivation (not the DB row), it survives every
# re-sync — which always refreshes capabilities. Matched as model_id prefixes.
_TOOL_CALLING_DENYLIST: tuple[str, ...] = ("ai21/",)


def _tool_calling_denied(model_id: str) -> bool:
    mid = (model_id or "").lower()
    return any(mid.startswith(prefix) for prefix in _TOOL_CALLING_DENYLIST)


def _extract_capabilities(raw: dict[str, Any], model_id: str) -> dict[str, bool]:
    """Derive the five capability flags from an OpenRouter model entry.

    Tolerant of missing/malformed fields — anything absent or unparseable
    yields False, so a thin upstream entry never crashes the sync. `supports_tools`
    additionally honors a denylist of models that falsely advertise tool support
    (tr-1kb).
    """
    architecture = raw.get("architecture") or {}
    modalities = architecture.get("input_modalities")
    if not isinstance(modalities, list):
        modalities = []

    supported = raw.get("supported_parameters")
    if not isinstance(supported, list):
        supported = []

    web_search_price = (raw.get("pricing") or {}).get("web_search")
    try:
        supports_web_search = (
            web_search_price is not None and float(web_search_price) > 0
        )
    except (TypeError, ValueError):
        supports_web_search = False

    return {
        "supports_image_input": "image" in modalities,
        "supports_file_input": "file" in modalities,
        "supports_tools": ("tools" in supported) and not _tool_calling_denied(model_id),
        "supports_reasoning": "reasoning" in supported,
        "supports_web_search": supports_web_search,
    }


class ModelSyncService:
    async def sync_models(self, db: AsyncSession, redis: Redis) -> SyncResult:
        result = SyncResult()

        try:
            api_key = await get_active_key(db)
        except Exception as exc:  # noqa: BLE001 — 503 (no key) or anything else
            result.errors.append(f"no active key: {exc}")
            return result

        try:
            models = await self._fetch_models(api_key)
        except httpx.HTTPError as exc:
            result.errors.append(f"fetch failed: {exc}")
            return result

        exchange_rate, markup = await _get_pricing_settings(db)

        existing_rows = (
            await db.execute(
                select(ModelPricing).where(ModelPricing.is_current.is_(True))
            )
        ).scalars().all()
        by_id = {row.model_id: row for row in existing_rows}

        seen_ids: set[str] = set()
        now = datetime.now(timezone.utc)

        for raw in models:
            model_id = raw.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            if model_id.endswith(":free"):
                continue
            # Dynamic-price routers and zero/broken prices never enter the
            # catalogue. Skipping leaves them out of seen_ids, so any stale row
            # is flagged unavailable by the missing-upstream pass below.
            if not _has_real_pricing(raw.get("pricing") or {}):
                continue

            try:
                self._upsert_model(
                    db, by_id, raw, model_id, exchange_rate, markup, now, result
                )
            except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
                result.errors.append(f"{model_id}: {exc}")
                continue
            seen_ids.add(model_id)

        # Models present in DB (and currently available) but absent upstream.
        for model_id, row in by_id.items():
            if model_id in seen_ids:
                continue
            if row.status == "available":
                row.status = "unavailable"
                result.deactivated += 1

        # One-time cleanup: any current row with a non-positive RUB input price is
        # legacy bad data (old dynamic-price routers). Flag unavailable rather than
        # delete, to preserve pricing history. Runs after the missing-upstream pass
        # so rows already deactivated there aren't counted twice.
        bad_price_rows = (
            await db.execute(
                select(ModelPricing).where(
                    ModelPricing.is_current.is_(True),
                    ModelPricing.status == "available",
                    ModelPricing.input_price_micro_rub <= 0,
                )
            )
        ).scalars().all()
        for row in bad_price_rows:
            row.status = "unavailable"
            result.deactivated += 1

        await db.commit()

        # Pricing changed — drop the read caches so the next request sees fresh data.
        await redis.delete(PRICING_CACHE_KEY)
        await redis.delete(PRICING_CURRENT_CACHE_KEY)

        return result

    async def _fetch_models(self, api_key: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=_MODELS_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{settings.openrouter_base_url}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            payload = resp.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, list) else []

    def _upsert_model(
        self,
        db: AsyncSession,
        by_id: dict[str, ModelPricing],
        raw: dict[str, Any],
        model_id: str,
        exchange_rate: int,
        markup: float,
        now: datetime,
        result: SyncResult,
    ) -> None:
        pricing = raw.get("pricing") or {}
        in_micro_usd = _to_micro_usd_per_million(pricing.get("prompt", "0"))
        out_micro_usd = _to_micro_usd_per_million(pricing.get("completion", "0"))

        context_length = raw.get("context_length")
        max_context = int(context_length) if context_length else 0
        name = raw.get("name") or model_id
        capabilities = _extract_capabilities(raw, model_id)

        row = by_id.get(model_id)
        if row is None:
            in_rub = calculate_rub_price(in_micro_usd, exchange_rate, markup)
            out_rub = calculate_rub_price(out_micro_usd, exchange_rate, markup)
            db.add(
                ModelPricing(
                    model_id=model_id,
                    model_name=name,
                    provider=_provider_from_model_id(model_id),
                    openrouter_input_price_micro_usd=in_micro_usd,
                    openrouter_output_price_micro_usd=out_micro_usd,
                    input_price_micro_rub=in_rub,
                    output_price_micro_rub=out_rub,
                    is_price_override=False,
                    max_context_tokens=max_context,
                    stream_timeout_seconds=_stream_timeout_for(model_id),
                    status="available",
                    is_current=True,
                    effective_from=now,
                    **capabilities,
                )
            )
            result.added += 1
            return

        # Update source USD prices + context in place. RUB prices are recomputed
        # only when the row is not a manual override, so admin price pins survive.
        row.openrouter_input_price_micro_usd = in_micro_usd
        row.openrouter_output_price_micro_usd = out_micro_usd
        row.model_name = name
        if max_context:
            row.max_context_tokens = max_context
        # Capabilities always refresh, even for price-override rows: the override
        # pins the price only, not what the model can do.
        for field_name, value in capabilities.items():
            setattr(row, field_name, value)
        # A previously-unavailable model that reappears upstream goes live again.
        if row.status == "unavailable":
            row.status = "available"
        if not row.is_price_override:
            row.input_price_micro_rub = calculate_rub_price(
                in_micro_usd, exchange_rate, markup
            )
            row.output_price_micro_rub = calculate_rub_price(
                out_micro_usd, exchange_rate, markup
            )
            # Admin-pinned rows are left untouched so manual timeout tuning survives.
            row.stream_timeout_seconds = _stream_timeout_for(model_id)
        result.updated += 1
