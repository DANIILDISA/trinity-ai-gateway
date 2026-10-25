import json
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import MICRO_PER_UNIT, micro_to_api_string
from app.core.redis import get_redis_client
from app.models.audit_log import AdminAuditLog
from app.models.model_pricing import ModelPricing
from app.models.system_setting import SystemSetting
from app.models.user import User

PRICING_CACHE_KEY = "pricing:all"
PRICING_CACHE_TTL_SECONDS = 60
# Per BILLING_LOGIC.md §8.1 step 7 — separate cache key the orchestrator hits.
PRICING_CURRENT_CACHE_KEY = "pricing:current"

_DEFAULT_EXCHANGE_RATE_MICRO_RUB = 82_500_000
_DEFAULT_MARKUP = 1.25
_DEFAULT_COOLDOWN_DAYS = 7


def calculate_rub_price(
    openrouter_price_micro_usd: int,
    exchange_rate_micro_rub: int,
    markup_multiplier: float,
) -> int:
    """RUB price per 1M tokens (micro-rubles), ceiling-rounded at every step.

    BILLING_LOGIC.md §8.1: ceil(usd_price × rate × markup / 10^6).
    """
    base_micro_rub = (
        openrouter_price_micro_usd * exchange_rate_micro_rub + MICRO_PER_UNIT - 1
    ) // MICRO_PER_UNIT
    # round(), not int(): 1.15 floats to 114.999... — int() would truncate to
    # 114 (15% off-by-one). round() preserves the operator's intended markup.
    markup_int = round(markup_multiplier * 100)
    return (base_micro_rub * markup_int + 99) // 100


# M4 (audit): cache the two BILLED pricing settings in Redis so the hot billing
# path skips a DB read. They are read once per request and threaded through
# estimate + finalize, so caching is PURE PERF — it never changes what is charged
# (read-once-per-request semantics are unchanged). `_set_setting` invalidates the
# keys on any rate/markup write so a change propagates immediately; the TTL is a
# backstop. Same `settings:` convention as the other cached settings; these two
# keys are cached by no one else.
_PRICING_SETTINGS_CACHE_TTL_SECONDS = 60
_RATE_CACHE_KEY = "settings:exchange_rate_micro_rub"
_MARKUP_CACHE_KEY = "settings:markup_multiplier"
# Settings whose write must bust the pricing cache (used by _set_setting).
_PRICING_CACHE_BY_SETTING = {
    "exchange_rate_micro_rub": _RATE_CACHE_KEY,
    "markup_multiplier": _MARKUP_CACHE_KEY,
}


async def _get_pricing_settings(db: AsyncSession) -> tuple[int, float]:
    redis = None
    try:
        redis = get_redis_client()
        cached = await redis.mget(_RATE_CACHE_KEY, _MARKUP_CACHE_KEY)
        if cached[0] is not None and cached[1] is not None:
            return int(cached[0]), float(cached[1])
    except Exception:  # noqa: BLE001 — pricing must never break on a cache hiccup
        pass

    rows = (
        await db.execute(
            select(SystemSetting.key, SystemSetting.value).where(
                SystemSetting.key.in_(
                    ["exchange_rate_micro_rub", "markup_multiplier"]
                )
            )
        )
    ).all()
    by_key = {k: v for k, v in rows}
    exchange_rate = int(
        by_key.get("exchange_rate_micro_rub") or _DEFAULT_EXCHANGE_RATE_MICRO_RUB
    )
    markup = float(by_key.get("markup_multiplier") or _DEFAULT_MARKUP)

    if redis is not None:
        try:
            await redis.set(
                _RATE_CACHE_KEY, str(exchange_rate),
                ex=_PRICING_SETTINGS_CACHE_TTL_SECONDS,
            )
            await redis.set(
                _MARKUP_CACHE_KEY, str(markup),
                ex=_PRICING_SETTINGS_CACHE_TTL_SECONDS,
            )
        except Exception:  # noqa: BLE001
            pass
    return exchange_rate, markup


def _row_to_public(row: ModelPricing) -> dict[str, Any]:
    # Display the BILLED price — the stored RUB columns that finalize actually
    # charges (billing_service uses row.input_price_micro_rub / output_price_micro_rub)
    # — never the live formula. This makes "displayed price == charged price" a
    # structural invariant: a price override (is_price_override) or any drift of the
    # live formula from the stored price (e.g. a markup-only settings change,
    # tr-2ubv.20) can no longer show the user a price different from what they are
    # billed (tr-bztb: ZZPP exposure / revenue leak). Non-override stored prices are
    # kept equal to the formula by the exchange-rate updater, so this is a no-op for
    # them today.
    return {
        "id": row.model_id,
        "name": row.model_name,
        "provider": row.provider,
        "input_price_per_m": micro_to_api_string(row.input_price_micro_rub),
        "output_price_per_m": micro_to_api_string(row.output_price_micro_rub),
        "context_window": row.max_context_tokens,
        "status": row.status,
        "supports_image_input": row.supports_image_input,
        "supports_file_input": row.supports_file_input,
        "supports_tools": row.supports_tools,
        "supports_reasoning": row.supports_reasoning,
        "supports_web_search": row.supports_web_search,
        "last_updated": row.effective_from.isoformat(),
    }


def _replacement_current_row(
    src: ModelPricing,
    *,
    input_price_micro_rub: int,
    output_price_micro_rub: int,
    is_price_override: bool,
    effective_from: datetime,
) -> ModelPricing:
    """Build the new ``is_current`` row that replaces ``src`` on a tariff or
    override event (which archive ``src`` and insert this fresh row).

    Carries over EVERY descriptive column — capability flags, context/output
    windows, and the per-model stream timeout — so a price change never resets
    them to their server_default. Only the price, the override flag, and the
    effective_from timestamp legitimately change here. (Regression: omitting the
    capability flags reset them to false on every exchange-rate update, silently
    disabling web search (supports_web_search) and extended thinking
    (supports_reasoning); the per-model stream timeout likewise reverted to 30s,
    cutting reasoning/deep-research models off mid-think.)
    """
    return ModelPricing(
        model_id=src.model_id,
        model_name=src.model_name,
        provider=src.provider,
        openrouter_input_price_micro_usd=src.openrouter_input_price_micro_usd,
        openrouter_output_price_micro_usd=src.openrouter_output_price_micro_usd,
        input_price_micro_rub=input_price_micro_rub,
        output_price_micro_rub=output_price_micro_rub,
        is_price_override=is_price_override,
        max_context_tokens=src.max_context_tokens,
        max_output_tokens=src.max_output_tokens,
        stream_timeout_seconds=src.stream_timeout_seconds,
        supports_image_input=src.supports_image_input,
        supports_file_input=src.supports_file_input,
        supports_tools=src.supports_tools,
        supports_reasoning=src.supports_reasoning,
        supports_web_search=src.supports_web_search,
        status=src.status,
        is_current=True,
        effective_from=effective_from,
    )


async def get_current_pricing(db: AsyncSession, model_id: str) -> dict[str, Any]:
    row = (
        await db.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == model_id,
                ModelPricing.is_current.is_(True),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Модель не найдена")

    return _row_to_public(row)


async def get_all_current_pricing(
    db: AsyncSession, redis: Redis
) -> list[dict[str, Any]]:
    cached = await redis.get(PRICING_CACHE_KEY)
    if cached:
        return json.loads(cached)

    rows = (
        await db.execute(
            select(ModelPricing)
            .where(
                ModelPricing.is_current.is_(True),
                ModelPricing.status == "available",
            )
            .order_by(ModelPricing.provider, ModelPricing.model_name)
        )
    ).scalars().all()

    result = [_row_to_public(r) for r in rows]
    try:
        await redis.setex(PRICING_CACHE_KEY, PRICING_CACHE_TTL_SECONDS, json.dumps(result))
    except RedisError:
        pass  # DB is source of truth — fail open on an OOM cache write (tr-5rd7)
    return result


@dataclass(frozen=True)
class TariffUpdateResult:
    applied: bool
    previous_rate_micro_rub: int
    new_rate_micro_rub: int
    models_updated: int
    next_update_allowed_at: datetime


async def _get_setting(
    db: AsyncSession, key: str
) -> Any:
    return (
        await db.execute(
            select(SystemSetting.value).where(SystemSetting.key == key)
        )
    ).scalar_one_or_none()


async def _set_setting(
    db: AsyncSession, key: str, value: Any, admin_id: _uuid.UUID | None = None
) -> None:
    """Upsert a system setting. JSONB column accepts JSON-serializable values."""
    stmt = pg_insert(SystemSetting).values(
        key=key,
        value=value,
        updated_at=datetime.now(timezone.utc),
        updated_by=admin_id,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={
            "value": stmt.excluded.value,
            "updated_at": stmt.excluded.updated_at,
            "updated_by": stmt.excluded.updated_by,
        },
    )
    await db.execute(stmt)
    # M4: bust the pricing-settings cache on any rate/markup write so the change
    # propagates immediately (the 60s TTL is only a backstop). All rate/markup
    # writes funnel through here. Best-effort: a cache failure must not fail the
    # write. (Invalidation runs pre-commit; a concurrent read in the tiny window
    # before the caller commits can re-cache the old value — bounded by the TTL.)
    cache_key = _PRICING_CACHE_BY_SETTING.get(key)
    if cache_key is not None:
        try:
            await get_redis_client().delete(cache_key)
        except Exception:  # noqa: BLE001
            pass


async def _get_cooldown_days(db: AsyncSession) -> int:
    raw = await _get_setting(db, "exchange_rate_cooldown_days")
    if raw is None:
        return _DEFAULT_COOLDOWN_DAYS
    return int(raw)


def _parse_iso(value: Any) -> datetime | None:
    """exchange_rate_updated_at is stored as a JSON string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        # Tolerate "...Z" — Python <3.11 doesn't accept Z directly.
        cleaned = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


async def create_tariff_change_notifications(
    db: AsyncSession,
    old_rate_micro_rub: int | None,
    new_rate_micro_rub: int,
) -> None:
    """Delegates to notification_service. BILLING_LOGIC.md §15.3."""
    from app.services.notification_service import (
        create_tariff_change_notifications as _fan_out,
    )

    await _fan_out(db, old_rate_micro_rub, new_rate_micro_rub)


async def update_exchange_rate(
    db: AsyncSession,
    redis: Redis,
    new_rate_micro_rub: int,
    admin_user_id: _uuid.UUID | None,
    *,
    enforce_cooldown: bool = False,
) -> TariffUpdateResult:
    """BILLING_LOGIC.md §8.1 — full 7-step flow.

    `admin_user_id` is None for the automated CBR updater (audit logged as a
    system action). `enforce_cooldown` is False by default: manual admin
    updates are always allowed (the cooldown only ever applied to the manual
    path and product now wants it gone). The automated updater self-throttles
    via `last_exchange_rate_update` instead, so it also passes False.
    Raises HTTPException(409) only when `enforce_cooldown=True` and within the
    cooldown window.
    """
    now = datetime.now(timezone.utc)
    cooldown_days = await _get_cooldown_days(db)

    if enforce_cooldown:
        last_updated = _parse_iso(
            await _get_setting(db, "exchange_rate_updated_at")
        )
        if last_updated is not None:
            elapsed = now - last_updated
            if elapsed.days < cooldown_days:
                next_allowed = last_updated + timedelta(days=cooldown_days)
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "cooldown",
                        "next_allowed_at": next_allowed.isoformat(),
                        "message": (
                            f"Тарифы можно обновлять раз в {cooldown_days} дней."
                        ),
                    },
                )

    old_rate_raw = await _get_setting(db, "exchange_rate_micro_rub")
    old_rate = (
        int(old_rate_raw) if old_rate_raw is not None
        else _DEFAULT_EXCHANGE_RATE_MICRO_RUB
    )
    _, markup = await _get_pricing_settings(db)

    await _set_setting(
        db, "exchange_rate_micro_rub", new_rate_micro_rub, admin_user_id
    )
    await _set_setting(
        db, "exchange_rate_updated_at", now.isoformat(), admin_user_id
    )

    rows = (
        await db.execute(
            select(ModelPricing).where(ModelPricing.is_current.is_(True))
        )
    ).scalars().all()

    models_updated = 0
    for row in rows:
        if row.is_price_override:
            continue

        new_input_micro = calculate_rub_price(
            row.openrouter_input_price_micro_usd, new_rate_micro_rub, markup
        )
        new_output_micro = calculate_rub_price(
            row.openrouter_output_price_micro_usd, new_rate_micro_rub, markup
        )

        row.is_current = False
        row.effective_until = now

        db.add(
            _replacement_current_row(
                row,
                input_price_micro_rub=new_input_micro,
                output_price_micro_rub=new_output_micro,
                is_price_override=False,
                effective_from=now,
            )
        )
        models_updated += 1

    # Audit log (BILLING_LOGIC §8.1 step 5). admin_user_id is None for the
    # automated CBR updater — logged as a system action with no actor.
    admin = (
        await db.execute(select(User).where(User.id == admin_user_id))
    ).scalar_one_or_none() if admin_user_id is not None else None
    db.add(
        AdminAuditLog(
            actor_id=admin_user_id,
            actor_email=admin.email if admin else None,
            action=(
                "admin.tariff.update_rate"
                if admin_user_id is not None
                else "system.tariff.auto_update"
            ),
            target_type="system",
            target_id="exchange_rate",
            details={
                "old_rate_micro_rub": old_rate,
                "new_rate_micro_rub": new_rate_micro_rub,
                "models_updated": models_updated,
            },
        )
    )

    await create_tariff_change_notifications(db, old_rate, new_rate_micro_rub)

    await db.commit()

    # Invalidate caches AFTER commit so a polling reader can't fetch the stale
    # cache and then immediately see new committed rows.
    await redis.delete(PRICING_CURRENT_CACHE_KEY)
    await redis.delete(PRICING_CACHE_KEY)

    return TariffUpdateResult(
        applied=True,
        previous_rate_micro_rub=old_rate,
        new_rate_micro_rub=new_rate_micro_rub,
        models_updated=models_updated,
        next_update_allowed_at=now + timedelta(days=cooldown_days),
    )


async def override_model_price(
    db: AsyncSession,
    redis: Redis,
    model_id: str,
    input_price_micro_rub: int,
    output_price_micro_rub: int,
    admin_user_id: _uuid.UUID,
) -> ModelPricing:
    """Pin a model's prices so future rate updates skip it.

    Archives the current row + inserts a new one with is_price_override=True.
    """
    now = datetime.now(timezone.utc)
    current = (
        await db.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == model_id,
                ModelPricing.is_current.is_(True),
            )
        )
    ).scalar_one_or_none()
    if current is None:
        raise HTTPException(status_code=404, detail="Модель не найдена")

    current.is_current = False
    current.effective_until = now

    new_row = _replacement_current_row(
        current,
        input_price_micro_rub=input_price_micro_rub,
        output_price_micro_rub=output_price_micro_rub,
        is_price_override=True,
        effective_from=now,
    )
    db.add(new_row)

    admin = (
        await db.execute(select(User).where(User.id == admin_user_id))
    ).scalar_one_or_none()
    db.add(
        AdminAuditLog(
            actor_id=admin_user_id,
            actor_email=admin.email if admin else None,
            action="admin.tariff.override_model",
            target_type="model_pricing",
            target_id=model_id,
            details={
                "input_price_micro_rub": input_price_micro_rub,
                "output_price_micro_rub": output_price_micro_rub,
            },
        )
    )

    await db.commit()
    await redis.delete(PRICING_CURRENT_CACHE_KEY)
    await redis.delete(PRICING_CACHE_KEY)
    return new_row


async def clear_model_price_override(
    db: AsyncSession,
    redis: Redis,
    model_id: str,
    admin_user_id: _uuid.UUID,
) -> ModelPricing:
    """Remove a model's price pin (BUG 3 — "Снять фиксацию").

    Archives the pinned row and inserts a fresh one with is_price_override=False
    and prices recomputed from the current exchange rate + markup, so the model
    rejoins automatic rate updates.
    """
    now = datetime.now(timezone.utc)
    current = (
        await db.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == model_id,
                ModelPricing.is_current.is_(True),
            )
        )
    ).scalar_one_or_none()
    if current is None:
        raise HTTPException(status_code=404, detail="Модель не найдена")
    if not current.is_price_override:
        raise HTTPException(status_code=400, detail="Цена не зафиксирована")

    exchange_rate, markup = await _get_pricing_settings(db)
    new_input = calculate_rub_price(
        current.openrouter_input_price_micro_usd, exchange_rate, markup
    )
    new_output = calculate_rub_price(
        current.openrouter_output_price_micro_usd, exchange_rate, markup
    )

    current.is_current = False
    current.effective_until = now

    new_row = _replacement_current_row(
        current,
        input_price_micro_rub=new_input,
        output_price_micro_rub=new_output,
        is_price_override=False,
        effective_from=now,
    )
    db.add(new_row)

    admin = (
        await db.execute(select(User).where(User.id == admin_user_id))
    ).scalar_one_or_none()
    db.add(
        AdminAuditLog(
            actor_id=admin_user_id,
            actor_email=admin.email if admin else None,
            action="admin.tariff.clear_override",
            target_type="model_pricing",
            target_id=model_id,
            details={
                "input_price_micro_rub": new_input,
                "output_price_micro_rub": new_output,
            },
        )
    )

    await db.commit()
    await redis.delete(PRICING_CURRENT_CACHE_KEY)
    await redis.delete(PRICING_CACHE_KEY)
    return new_row


async def preview_tariff(
    db: AsyncSession, new_rate_micro_rub: int
) -> list[dict[str, Any]]:
    _, markup = await _get_pricing_settings(db)
    rows = (
        await db.execute(
            select(ModelPricing).where(ModelPricing.is_current.is_(True))
        )
    ).scalars().all()

    previews = []
    for row in rows:
        new_input = calculate_rub_price(
            row.openrouter_input_price_micro_usd, new_rate_micro_rub, markup
        )
        new_output = calculate_rub_price(
            row.openrouter_output_price_micro_usd, new_rate_micro_rub, markup
        )
        previews.append({
            "model_id": row.model_id,
            "model_name": row.model_name,
            "current_input_price": micro_to_api_string(row.input_price_micro_rub),
            "current_output_price": micro_to_api_string(row.output_price_micro_rub),
            "proposed_input_price": micro_to_api_string(new_input),
            "proposed_output_price": micro_to_api_string(new_output),
            "is_override": row.is_price_override,
        })
    return previews
