"""Admin system settings endpoints (Task 8.6).

GET /api/v1/admin/settings        — grouped read of all whitelisted keys
PUT /api/v1/admin/settings/{key}  — single-key update (audit-logged, cache-invalidating)
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_redis, require_admin
from app.models.audit_log import AdminAuditLog
from app.models.system_setting import SystemSetting
from app.models.user import User
from app.services.tariff_service import _set_setting

router = APIRouter()


# Whitelist of writable keys + their group + expected value type. Keys not in
# this map are rejected so admins can't insert arbitrary settings rows.
class _KeyMeta:
    __slots__ = ("group", "py_type")

    def __init__(self, group: str, py_type: type) -> None:
        self.group = group
        self.py_type = py_type


_KEYS: dict[str, _KeyMeta] = {
    "rate_limit_per_minute": _KeyMeta("rate_limits", int),
    "rate_limit_per_hour": _KeyMeta("rate_limits", int),
    "rate_limit_per_day": _KeyMeta("rate_limits", int),
    "reservation_buffer_multiplier": _KeyMeta("billing", float),
    "overdraft_tolerance_percent": _KeyMeta("billing", float),
    "low_balance_remaining_requests": _KeyMeta("billing", int),
    # Pricing markup multiplier (1.25 == +25%). Editable in the admin UI.
    "markup_multiplier": _KeyMeta("billing", float),
    "min_topup_amount_rub": _KeyMeta("payments", int),
    "openrouter_balance_threshold_usd": _KeyMeta("openrouter", float),
    "reconciliation_threshold_percent": _KeyMeta("reconciliation", float),
    # CBR auto-update controls.
    "auto_exchange_rate_enabled": _KeyMeta("exchange_rate", bool),
    "auto_exchange_rate_interval_minutes": _KeyMeta("exchange_rate", int),
    "auto_exchange_rate_mode": _KeyMeta("exchange_rate", str),
}

_EXCHANGE_RATE_MODES = {"auto", "manual_confirm"}


class SettingsResponse(BaseModel):
    rate_limits: dict[str, Any]
    billing: dict[str, Any]
    payments: dict[str, Any]
    openrouter: dict[str, Any]
    reconciliation: dict[str, Any]
    exchange_rate: dict[str, Any]


class UpdateSettingRequest(BaseModel):
    value: Any


class UpdateSettingResponse(BaseModel):
    key: str
    value: Any
    updated_at: datetime


class OpenRouterBalanceRequest(BaseModel):
    balance_usd: float


class OpenRouterBalanceResponse(BaseModel):
    balance_usd: float
    updated_at: datetime


# ── Helpers ─────────────────────────────────────────────────────────────────


def _coerce(meta: _KeyMeta, value: Any) -> Any:
    if meta.py_type is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        if isinstance(value, (int, float)):
            return bool(value)
        raise HTTPException(status_code=422, detail="Ожидается true/false.")
    if meta.py_type is int:
        if isinstance(value, bool):
            raise HTTPException(status_code=422, detail="Ожидается число.")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Ожидается целое число.")
    if meta.py_type is float:
        try:
            return float(value)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422, detail="Ожидается число."
            )
    if meta.py_type is str:
        return str(value)
    return value


def _audit(
    db: AsyncSession,
    admin: User,
    key: str,
    old_value: Any,
    new_value: Any,
) -> None:
    db.add(
        AdminAuditLog(
            actor_id=admin.id,
            actor_email=admin.email,
            action="admin.settings.update",
            target_type="system_setting",
            target_id=key,
            details={"key": key, "old_value": old_value, "new_value": new_value},
        )
    )


async def _invalidate_caches(redis: Redis, key: str) -> None:
    """Clear the per-setting Redis cache used by rate_limiter, plus the bulk
    cache key the task spec asks for."""
    await redis.delete(f"settings:{key}")
    if key.startswith("rate_limit_"):
        await redis.delete("rate_limits:cache")


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get("", response_model=SettingsResponse)
async def get_settings(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SettingsResponse:
    rows = (
        await db.execute(
            select(SystemSetting.key, SystemSetting.value).where(
                SystemSetting.key.in_(list(_KEYS.keys()))
            )
        )
    ).all()
    by_key = {k: v for k, v in rows}

    grouped: dict[str, dict[str, Any]] = {
        "rate_limits": {},
        "billing": {},
        "payments": {},
        "openrouter": {},
        "reconciliation": {},
        "exchange_rate": {},
    }
    for key, meta in _KEYS.items():
        raw = by_key.get(key)
        coerced = _coerce(meta, raw) if raw is not None else None
        grouped[meta.group][key] = coerced

    return SettingsResponse(**grouped)


@router.put("/openrouter-balance", response_model=OpenRouterBalanceResponse)
async def update_openrouter_balance(
    body: OpenRouterBalanceRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> OpenRouterBalanceResponse:
    """Manually set the OpenRouter account balance (USD).

    OpenRouter doesn't expose balance to provisioned keys, so the dashboard
    falls back to this admin-entered value. Stored as a Decimal string in
    system_settings under openrouter_manual_balance_usd.
    """
    if body.balance_usd < 0:
        raise HTTPException(status_code=422, detail="Баланс не может быть отрицательным.")

    value = str(Decimal(str(body.balance_usd)))
    old_raw = (
        await db.execute(
            select(SystemSetting.value).where(
                SystemSetting.key == "openrouter_manual_balance_usd"
            )
        )
    ).scalar_one_or_none()

    await _set_setting(db, "openrouter_manual_balance_usd", value, admin.id)
    _audit(db, admin, "openrouter_manual_balance_usd", old_raw, value)
    await db.commit()

    return OpenRouterBalanceResponse(
        balance_usd=body.balance_usd,
        updated_at=datetime.now(timezone.utc),
    )


@router.put("/{key}", response_model=UpdateSettingResponse)
async def update_setting(
    key: str,
    body: UpdateSettingRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> UpdateSettingResponse:
    meta = _KEYS.get(key)
    if meta is None:
        raise HTTPException(status_code=400, detail="Неизвестный ключ настройки.")

    new_value = _coerce(meta, body.value)
    if key == "auto_exchange_rate_mode" and new_value not in _EXCHANGE_RATE_MODES:
        raise HTTPException(
            status_code=422,
            detail="Режим должен быть 'auto' или 'manual_confirm'.",
        )
    old_raw = (
        await db.execute(
            select(SystemSetting.value).where(SystemSetting.key == key)
        )
    ).scalar_one_or_none()
    old_value = _coerce(meta, old_raw) if old_raw is not None else None

    await _set_setting(db, key, new_value, admin.id)
    _audit(db, admin, key, old_value, new_value)
    await db.commit()
    await _invalidate_caches(redis, key)

    return UpdateSettingResponse(
        key=key,
        value=new_value,
        updated_at=datetime.now(timezone.utc),
    )
