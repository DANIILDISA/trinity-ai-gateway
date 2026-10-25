"""Automated CBR exchange-rate updater.

Designed to be invoked once per minute by cron; it self-throttles to
`auto_exchange_rate_interval_minutes` using the `last_exchange_rate_update`
timestamp in system_settings, so most invocations are cheap no-ops.

Behaviour, gated by system_settings:
  auto_exchange_rate_enabled (bool, default false)
      Master switch. If false → do nothing.
  auto_exchange_rate_interval_minutes (int, default 1440)
      Minimum minutes between real updates.
  auto_exchange_rate_mode ("auto" | "manual_confirm", default "auto")
      "auto"           → fetch + apply immediately.
      "manual_confirm" → fetch + raise an `exchange_rate` alert with the
                         proposed rate in details; an admin applies/dismisses.

DEMO_MODE fetches the rate but logs instead of saving.

Run standalone:
    docker compose exec -T backend python -m app.tasks.exchange_rate
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.constants import MICRO_PER_UNIT
from app.core.database import dispose_engine, get_session_factory, init_engine
from app.core.redis import close_redis, get_redis_pool, init_redis
from app.services.alert_service import create_alert
from app.services.cbr_service import fetch_cbr_usd_rate
from app.services.tariff_service import (
    _DEFAULT_EXCHANGE_RATE_MICRO_RUB,
    _get_setting,
    _parse_iso,
    _set_setting,
    update_exchange_rate,
)

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_MINUTES = 1440  # daily
LAST_UPDATE_KEY = "last_exchange_rate_update"


def _rate_to_micro_rub(rate: Decimal) -> int:
    return int(
        (rate * MICRO_PER_UNIT).to_integral_value(rounding=ROUND_HALF_UP)
    )


async def _get_bool(db: AsyncSession, key: str, default: bool) -> bool:
    raw = await _get_setting(db, key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


async def _get_int(db: AsyncSession, key: str, default: int) -> int:
    raw = await _get_setting(db, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


async def _get_str(db: AsyncSession, key: str, default: str) -> str:
    raw = await _get_setting(db, key)
    return str(raw) if raw is not None else default


async def run_exchange_rate_update(
    db: AsyncSession, redis: Redis
) -> dict:
    """Fetch the CBR rate and apply/queue it per settings. Returns a small
    status dict (handy for tests and the standalone runner's log line)."""
    if not await _get_bool(db, "auto_exchange_rate_enabled", False):
        logger.info("Auto exchange-rate update disabled — skipping.")
        return {"status": "disabled"}

    interval = await _get_int(
        db, "auto_exchange_rate_interval_minutes", DEFAULT_INTERVAL_MINUTES
    )
    now = datetime.now(timezone.utc)
    last = _parse_iso(await _get_setting(db, LAST_UPDATE_KEY))
    if last is not None:
        elapsed_minutes = (now - last).total_seconds() / 60
        if elapsed_minutes < interval:
            logger.info(
                "Auto exchange-rate: %.1f/%d min since last update — skipping.",
                elapsed_minutes,
                interval,
            )
            return {"status": "throttled", "elapsed_minutes": elapsed_minutes}

    rate = await fetch_cbr_usd_rate()
    if rate is None:
        logger.warning("Auto exchange-rate: CBR fetch failed — nothing applied.")
        return {"status": "fetch_failed"}

    new_micro_rub = _rate_to_micro_rub(rate)

    if settings.demo_mode:
        logger.info(
            "[DEMO] Auto exchange-rate: CBR USD=%s (%d μ₽) — not saved.",
            rate,
            new_micro_rub,
        )
        return {"status": "demo", "rate_usd_rub": str(rate), "micro_rub": new_micro_rub}

    mode = await _get_str(db, "auto_exchange_rate_mode", "auto")

    # Record the attempt timestamp regardless of mode so the interval throttle
    # works for both auto-apply and manual-confirm (otherwise manual_confirm
    # would raise a fresh pending alert every single minute).
    await _set_setting(db, LAST_UPDATE_KEY, now.isoformat())

    if mode == "manual_confirm":
        current_raw = await _get_setting(db, "exchange_rate_micro_rub")
        current_micro = (
            int(current_raw)
            if current_raw is not None
            else _DEFAULT_EXCHANGE_RATE_MICRO_RUB
        )
        await create_alert(
            db,
            severity="warning",
            source="exchange_rate",
            title=f"Новый курс ЦБ к подтверждению: {rate} ₽/USD",
            message=(
                "Доступен новый курс ЦБ. Требуется подтверждение администратора."
            ),
            details={
                "type": "exchange_rate_pending",
                "rate_usd_rub": str(rate),
                "new_rate_micro_rub": new_micro_rub,
                "current_rate_micro_rub": current_micro,
            },
        )
        await db.commit()
        logger.info(
            "Auto exchange-rate: pending alert created (rate=%s, %d μ₽).",
            rate,
            new_micro_rub,
        )
        return {
            "status": "pending",
            "rate_usd_rub": str(rate),
            "micro_rub": new_micro_rub,
        }

    # mode == "auto": apply immediately (system actor, no cooldown).
    result = await update_exchange_rate(
        db, redis, new_micro_rub, admin_user_id=None
    )
    logger.info(
        "Auto exchange-rate applied: %s ₽/USD (%d μ₽), %d models updated.",
        rate,
        new_micro_rub,
        result.models_updated,
    )
    return {
        "status": "applied",
        "rate_usd_rub": str(rate),
        "micro_rub": new_micro_rub,
        "models_updated": result.models_updated,
    }


async def _main() -> None:
    init_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )
    init_redis(settings.redis_url)
    redis = Redis(connection_pool=get_redis_pool())
    try:
        factory = get_session_factory()
        async with factory() as db:
            await run_exchange_rate_update(db, redis)
    finally:
        await redis.aclose()
        await close_redis()
        await dispose_engine()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_main())
