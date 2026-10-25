"""Admin tariff management endpoints (Task 8.4).

GET    /api/v1/admin/tariffs                   — current rate + per-model prices
POST   /api/v1/admin/tariffs/preview           — proposed prices at a new rate
POST   /api/v1/admin/tariffs/apply             — commit a new rate (cooldown-aware)
PATCH  /api/v1/admin/tariffs/models/{id}       — pin a model's price (override)
GET    /api/v1/admin/tariffs/cbr-rate          — reference CBR rate (mock for MVP)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import micro_to_api_string
from app.dependencies import get_db, get_redis, require_admin
from app.models.alert import Alert
from app.models.model_pricing import ModelPricing
from app.models.user import User
from app.services.tariff_service import (
    _get_cooldown_days,
    _get_pricing_settings,
    _get_setting,
    _parse_iso,
    clear_model_price_override,
    override_model_price,
    preview_tariff,
    update_exchange_rate,
)

router = APIRouter()


# ── Schemas ─────────────────────────────────────────────────────────────────


class TariffModelRow(BaseModel):
    model_id: str
    model_name: str
    provider: str
    input_price_micro_rub: int
    input_price_display: str
    output_price_micro_rub: int
    output_price_display: str
    is_price_override: bool
    status: str


class TariffsResponse(BaseModel):
    exchange_rate_micro_rub: int
    exchange_rate_display: str
    last_updated: str | None
    next_update_allowed_at: str | None
    markup_multiplier: float
    models: list[TariffModelRow]


class PricePreviewRow(BaseModel):
    model_id: str
    model_name: str
    current_input_price: str
    current_output_price: str
    proposed_input_price: str
    proposed_output_price: str
    is_override: bool


class PreviewRequest(BaseModel):
    usd_rub_rate: int = Field(
        gt=0, description="Micro-rubles per 1 USD, e.g. 82500000."
    )


class ApplyRequest(PreviewRequest):
    pass


class ApplyResponse(BaseModel):
    applied: bool
    previous_rate_micro_rub: int
    previous_rate_display: str
    new_rate_micro_rub: int
    new_rate_display: str
    models_updated: int
    next_update_allowed_at: str


class ConfirmRateRequest(BaseModel):
    alert_id: uuid.UUID


class OverrideRequest(BaseModel):
    input_price_micro_rub: int = Field(ge=0)
    output_price_micro_rub: int = Field(ge=0)


class OverrideResponse(BaseModel):
    model_id: str
    input_price_micro_rub: int
    input_price_display: str
    output_price_micro_rub: int
    output_price_display: str
    is_price_override: bool


class CbrRateResponse(BaseModel):
    rate_usd_rub: float
    source: str
    fetched_at: str
    note: str | None = None


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get("", response_model=TariffsResponse)
async def get_tariffs(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> TariffsResponse:
    exchange_rate, markup = await _get_pricing_settings(db)
    cooldown_days = await _get_cooldown_days(db)
    last_updated = _parse_iso(await _get_setting(db, "exchange_rate_updated_at"))

    rows = (
        await db.execute(
            select(ModelPricing)
            .where(ModelPricing.is_current.is_(True))
            .order_by(ModelPricing.provider, ModelPricing.model_name)
        )
    ).scalars().all()

    models = [
        TariffModelRow(
            model_id=r.model_id,
            model_name=r.model_name,
            provider=r.provider,
            input_price_micro_rub=r.input_price_micro_rub,
            input_price_display=micro_to_api_string(r.input_price_micro_rub),
            output_price_micro_rub=r.output_price_micro_rub,
            output_price_display=micro_to_api_string(r.output_price_micro_rub),
            is_price_override=r.is_price_override,
            status=r.status,
        )
        for r in rows
    ]

    next_allowed_iso: str | None = None
    if last_updated is not None:
        next_allowed_iso = (
            last_updated + timedelta(days=cooldown_days)
        ).isoformat()

    return TariffsResponse(
        exchange_rate_micro_rub=exchange_rate,
        exchange_rate_display=micro_to_api_string(exchange_rate),
        last_updated=last_updated.isoformat() if last_updated else None,
        next_update_allowed_at=next_allowed_iso,
        markup_multiplier=markup,
        models=models,
    )


@router.post("/preview", response_model=list[PricePreviewRow])
async def preview_tariff_endpoint(
    body: PreviewRequest,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[PricePreviewRow]:
    raw = await preview_tariff(db, body.usd_rub_rate)
    return [PricePreviewRow(**row) for row in raw]


@router.post("/apply", response_model=ApplyResponse)
async def apply_tariff(
    body: ApplyRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> ApplyResponse:
    result = await update_exchange_rate(db, redis, body.usd_rub_rate, admin.id)
    return ApplyResponse(
        applied=result.applied,
        previous_rate_micro_rub=result.previous_rate_micro_rub,
        previous_rate_display=micro_to_api_string(result.previous_rate_micro_rub),
        new_rate_micro_rub=result.new_rate_micro_rub,
        new_rate_display=micro_to_api_string(result.new_rate_micro_rub),
        models_updated=result.models_updated,
        next_update_allowed_at=result.next_update_allowed_at.isoformat(),
    )


@router.post("/confirm-rate", response_model=ApplyResponse)
async def confirm_rate(
    body: ConfirmRateRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> ApplyResponse:
    """Apply the pending CBR rate carried by a manual-confirm alert, then resolve it.

    In `manual_confirm` mode the automated updater raises an `exchange_rate`
    alert whose details hold the proposed rate (`new_rate_micro_rub`).
    Confirming applies that rate exactly like POST /tariffs/apply and marks the
    alert resolved in the same request.
    """
    alert = (
        await db.execute(select(Alert).where(Alert.id == body.alert_id))
    ).scalar_one_or_none()
    if alert is None or alert.source != "exchange_rate":
        raise HTTPException(status_code=404, detail="Алерт с курсом ЦБ не найден")

    new_rate = (alert.details or {}).get("new_rate_micro_rub")
    if new_rate is None:
        raise HTTPException(
            status_code=400, detail="В алерте отсутствует предложенный курс"
        )

    result = await update_exchange_rate(db, redis, int(new_rate), admin.id)

    # update_exchange_rate already committed the rate change; now resolve the
    # alert so the pending notice clears from the admin list.
    alert.status = "resolved"
    alert.resolved_at = datetime.now(timezone.utc)
    alert.resolved_by = admin.id
    await db.commit()

    return ApplyResponse(
        applied=result.applied,
        previous_rate_micro_rub=result.previous_rate_micro_rub,
        previous_rate_display=micro_to_api_string(result.previous_rate_micro_rub),
        new_rate_micro_rub=result.new_rate_micro_rub,
        new_rate_display=micro_to_api_string(result.new_rate_micro_rub),
        models_updated=result.models_updated,
        next_update_allowed_at=result.next_update_allowed_at.isoformat(),
    )


@router.patch("/models/{model_id:path}", response_model=OverrideResponse)
async def override_model_price_endpoint(
    model_id: str,
    body: OverrideRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> OverrideResponse:
    new_row = await override_model_price(
        db,
        redis,
        model_id,
        body.input_price_micro_rub,
        body.output_price_micro_rub,
        admin.id,
    )
    return OverrideResponse(
        model_id=new_row.model_id,
        input_price_micro_rub=new_row.input_price_micro_rub,
        input_price_display=micro_to_api_string(new_row.input_price_micro_rub),
        output_price_micro_rub=new_row.output_price_micro_rub,
        output_price_display=micro_to_api_string(new_row.output_price_micro_rub),
        is_price_override=new_row.is_price_override,
    )


@router.delete("/models/{model_id:path}/override", response_model=OverrideResponse)
async def clear_model_price_override_endpoint(
    model_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> OverrideResponse:
    new_row = await clear_model_price_override(db, redis, model_id, admin.id)
    return OverrideResponse(
        model_id=new_row.model_id,
        input_price_micro_rub=new_row.input_price_micro_rub,
        input_price_display=micro_to_api_string(new_row.input_price_micro_rub),
        output_price_micro_rub=new_row.output_price_micro_rub,
        output_price_display=micro_to_api_string(new_row.output_price_micro_rub),
        is_price_override=new_row.is_price_override,
    )


@router.get("/cbr-rate", response_model=CbrRateResponse)
async def get_cbr_rate(
    _: User = Depends(require_admin),
) -> CbrRateResponse:
    return CbrRateResponse(
        rate_usd_rub=82.50,
        source="mock",
        fetched_at=datetime.now(timezone.utc).isoformat(),
        note="Курс загружается автоматически с сайта ЦБ РФ (cbr.ru)",
    )
