import hashlib
import hmac
import json
import uuid
from decimal import Decimal
from typing import Any

import httpx
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.constants import MICRO_PER_UNIT
from app.models.alert import Alert
from app.models.system_setting import SystemSetting
from app.schemas.billing import PaymentCreateResponse, PaymentStatusResponse
from app.services.billing_service import credit_balance
from app.services.notification_service import create_topup_notification

YOOKASSA_PAYMENTS_URL = "https://api.yookassa.ru/v3/payments"
_DEFAULT_MIN_TOPUP_RUB = 500


async def _get_min_topup_rub(db: AsyncSession) -> int:
    value = (
        await db.execute(
            select(SystemSetting.value).where(
                SystemSetting.key == "min_topup_amount_rub"
            )
        )
    ).scalar_one_or_none()
    if value is None:
        return _DEFAULT_MIN_TOPUP_RUB
    # JSONB '500' deserializes to int; '"500"' would be str — handle both defensively.
    return int(value)


async def create_payment(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount_rub: int,
) -> PaymentCreateResponse:
    min_amount = await _get_min_topup_rub(db)
    if amount_rub < min_amount:
        raise HTTPException(
            status_code=422,
            detail=f"Минимальная сумма пополнения — {min_amount} ₽",
        )

    amount_str = f"{amount_rub}.00"
    body = {
        "method": {"type": "sbp"},
        "amount": {"value": amount_str, "currency": "RUB"},
        "confirmation": {
            "type": "redirect",
            "return_url": settings.frontend_url,
        },
        "metadata": {"user_id": str(user_id)},
    }
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    auth = (settings.yookassa_shop_id, settings.yookassa_secret_key)

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            YOOKASSA_PAYMENTS_URL,
            json=body,
            headers=headers,
            auth=auth,
        )
        response.raise_for_status()
        data = response.json()

    return PaymentCreateResponse(
        payment_id=data["id"],
        confirmation_url=data["confirmation"]["confirmation_url"],
        status="pending",
        amount=amount_str,
    )


def _compute_signature(raw_body: bytes) -> str:
    return hmac.new(
        settings.yookassa_secret_key.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()


async def handle_webhook(
    db: AsyncSession,
    redis: Redis,
    raw_body: bytes,
    signature: str,
) -> dict[str, Any]:
    expected = _compute_signature(raw_body)
    if not signature or not hmac.compare_digest(expected, signature):
        db.add(Alert(
            severity="warning",
            source="payments",
            title="Invalid webhook signature",
            message="Webhook signature verification failed.",
            details={"body_length": len(raw_body)},
        ))
        await db.commit()
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(raw_body)
    obj = payload.get("object", {})
    status = obj.get("status")

    if status != "succeeded":
        return {"status": "ok", "duplicate": False, "processed": False}

    payment_id = obj["id"]
    amount_value = obj["amount"]["value"]
    user_id = uuid.UUID(obj["metadata"]["user_id"])
    amount_micro_rub = int(Decimal(amount_value) * MICRO_PER_UNIT)

    new_balance, was_applied = await credit_balance(
        db,
        user_id=user_id,
        amount_micro_rub=amount_micro_rub,
        idempotency_key=payment_id,
        description="Пополнение баланса",
        payment_id=payment_id,
    )

    if not was_applied:
        await db.commit()
        return {"status": "ok", "duplicate": True}

    await create_topup_notification(
        db, user_id, amount_micro_rub, new_balance
    )
    await db.commit()

    return {"status": "ok", "duplicate": False}


async def check_payment_status(
    user_id: uuid.UUID,
    payment_id: str,
) -> PaymentStatusResponse:
    auth = (settings.yookassa_shop_id, settings.yookassa_secret_key)
    url = f"{YOOKASSA_PAYMENTS_URL}/{payment_id}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url, auth=auth)
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="Платёж не найден")
        response.raise_for_status()
        data = response.json()

    metadata_user_id = (data.get("metadata") or {}).get("user_id")
    if metadata_user_id != str(user_id):
        # Do not reveal that the payment exists but belongs to another user.
        raise HTTPException(status_code=404, detail="Платёж не найден")

    return PaymentStatusResponse(
        payment_id=data["id"],
        status=data["status"],
        amount=data["amount"]["value"],
    )
