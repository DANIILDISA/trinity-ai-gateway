"""Periodic OpenRouter health probe (Task 6.4).

Stores account balance + per-model availability in Redis with a 6-minute TTL.
Triggers a critical Alert when balance drops below the configured threshold
(system_settings.openrouter_balance_threshold_usd).

SPEC.md §5.1 says we hit OpenRouter every 5 minutes; the 6-min TTL gives one
cycle of slack so the cache never goes stale between successful probes.
"""
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.alert import Alert
from app.models.model_pricing import ModelPricing
from app.models.system_setting import SystemSetting
from app.services.openrouter_proxy import OPENROUTER_BASE_URL, get_active_key

logger = logging.getLogger(__name__)

BALANCE_KEY = "openrouter:balance"
MODEL_HEALTH_KEY_PREFIX = "model_health:"
HEALTH_TTL_SECONDS = 360
_HTTP_TIMEOUT_SECONDS = 10.0

_MOCK_BALANCE_USD = 500.0


def _is_mock_key(key: str) -> bool:
    return key.startswith("mock-") or key == "test"


async def _fetch_openrouter_balance(api_key: str) -> float:
    """Return account remaining balance in USD via GET /auth/key.

    OpenRouter response shape: {"data": {"limit": float, "usage": float,
    "limit_remaining": float}}. limit_remaining can be None on unlimited
    accounts — fall back to (limit - usage) when so.
    """
    if _is_mock_key(api_key):
        return _MOCK_BALANCE_USD

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.get(
            f"{OPENROUTER_BASE_URL}/auth/key",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    resp.raise_for_status()
    data = resp.json().get("data", {})
    remaining = data.get("limit_remaining")
    if remaining is None:
        limit = data.get("limit") or 0
        usage = data.get("usage") or 0
        remaining = max(limit - usage, 0)
    return float(remaining)


async def _get_threshold_usd(db: AsyncSession) -> float:
    raw = (
        await db.execute(
            select(SystemSetting.value).where(
                SystemSetting.key == "openrouter_balance_threshold_usd"
            )
        )
    ).scalar_one_or_none()
    return float(raw) if raw is not None else 100.0


async def _get_manual_balance_usd(db: AsyncSession) -> float | None:
    """Admin-entered openrouter_manual_balance_usd, or None — same read as the
    dashboard's _get_manual_balance (app/api/v1/admin/dashboard.py)."""
    raw = (
        await db.execute(
            select(SystemSetting.value).where(
                SystemSetting.key == "openrouter_manual_balance_usd"
            )
        )
    ).scalar_one_or_none()
    if raw is None:
        return None
    try:
        return float(Decimal(str(raw)))
    except (InvalidOperation, TypeError, ValueError):
        return None


async def _current_model_ids(db: AsyncSession) -> list[str]:
    rows = (
        await db.execute(
            select(ModelPricing.model_id).where(
                ModelPricing.is_current.is_(True),
                ModelPricing.status != "deprecated",
            )
        )
    ).all()
    return [row[0] for row in rows]


async def _mark_all_unavailable(redis: Redis, db: AsyncSession) -> None:
    for model_id in await _current_model_ids(db):
        await redis.setex(
            f"{MODEL_HEALTH_KEY_PREFIX}{model_id}",
            HEALTH_TTL_SECONDS,
            "unavailable",
        )


async def _create_low_balance_alert(
    db: AsyncSession, balance_usd: float, threshold_usd: float
) -> None:
    db.add(Alert(
        severity="critical",
        source="openrouter",
        title="OpenRouter balance low",
        message=(
            f"Balance ${balance_usd:.2f} is below threshold "
            f"${threshold_usd:.2f}"
        ),
        details={
            "balance_usd": balance_usd,
            "threshold_usd": threshold_usd,
        },
    ))
    await db.commit()


async def check_openrouter_health(db: AsyncSession, redis: Redis) -> dict[str, Any]:
    """Probe OpenRouter, cache results, alert on low balance.

    Returns a small dict for the caller's diagnostics (also used by tests):
      {"balance_usd": float | None, "models_marked": int, "error": str | None}.
    Never raises — exceptions are caught so the periodic loop survives a flaky
    OpenRouter or a missing API key.
    """
    # DEMO_MODE: there is no real OpenRouter key in dev. Mark every current
    # model 'available' and cache a fake balance so the admin dashboard /
    # model list don't show everything as broken.
    if settings.demo_mode:
        model_ids = await _current_model_ids(db)
        for model_id in model_ids:
            await redis.setex(
                f"{MODEL_HEALTH_KEY_PREFIX}{model_id}",
                HEALTH_TTL_SECONDS,
                "available",
            )
        await redis.setex(
            BALANCE_KEY, HEALTH_TTL_SECONDS, f"{_MOCK_BALANCE_USD:.4f}"
        )
        return {
            "balance_usd": _MOCK_BALANCE_USD,
            "models_marked": len(model_ids),
            "error": None,
        }

    try:
        api_key = await get_active_key(db)
    except HTTPException as exc:
        # No active primary key — billing is already 503ing for users; here we
        # just flag every model as unavailable so the model list reflects it.
        logger.warning("Health check: no active OpenRouter key (%s)", exc.detail)
        await _mark_all_unavailable(redis, db)
        return {"balance_usd": None, "models_marked": 0, "error": "no_active_key"}

    try:
        balance_usd = await _fetch_openrouter_balance(api_key)
    except Exception as exc:  # noqa: BLE001 — surface as cached state
        logger.warning("Health check: balance probe failed: %s", exc)
        await _mark_all_unavailable(redis, db)
        return {"balance_usd": None, "models_marked": 0, "error": "probe_failed"}

    await redis.setex(BALANCE_KEY, HEALTH_TTL_SECONDS, f"{balance_usd:.4f}")

    model_ids = await _current_model_ids(db)
    for model_id in model_ids:
        await redis.setex(
            f"{MODEL_HEALTH_KEY_PREFIX}{model_id}",
            HEALTH_TTL_SECONDS,
            "available",
        )

    threshold_usd = await _get_threshold_usd(db)
    # OpenRouter doesn't report balance to provisioned keys, so the live probe
    # is often 0. For the alert decision only, fall back to the admin-entered
    # manual balance — the cached/displayed balance (BALANCE_KEY above) stays
    # the live value; only the threshold comparison uses the fallback.
    balance_for_alert = balance_usd
    if balance_usd is None or balance_usd == 0:
        manual = await _get_manual_balance_usd(db)
        if manual is not None:
            balance_for_alert = manual

    if balance_for_alert is not None and balance_for_alert < threshold_usd:
        await _create_low_balance_alert(db, balance_for_alert, threshold_usd)

    return {
        "balance_usd": balance_usd,
        "models_marked": len(model_ids),
        "error": None,
    }
