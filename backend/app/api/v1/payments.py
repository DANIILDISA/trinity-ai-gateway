from typing import Any

from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_redis, require_active, require_auth
from app.models.user import User
from app.schemas.billing import (
    PaymentCreateRequest,
    PaymentCreateResponse,
    PaymentStatusResponse,
)
from app.services.payment_service import (
    check_payment_status,
    create_payment,
    handle_webhook,
)

router = APIRouter()


@router.post("/create", response_model=PaymentCreateResponse)
async def create_payment_endpoint(
    body: PaymentCreateRequest,
    user: User = Depends(require_active),
    db: AsyncSession = Depends(get_db),
) -> PaymentCreateResponse:
    return await create_payment(db, user.id, body.amount)


@router.post("/webhook")
async def yookassa_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    raw_body = await request.body()
    signature = request.headers.get("X-YooKassa-Signature", "")
    return await handle_webhook(db, redis, raw_body, signature)


@router.get("/{payment_id}/status", response_model=PaymentStatusResponse)
async def get_payment_status(
    payment_id: str,
    user: User = Depends(require_auth),
) -> PaymentStatusResponse:
    return await check_payment_status(user.id, payment_id)
