from typing import Any

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_redis
from app.services.tariff_service import get_all_current_pricing
from app.tasks.health_check import MODEL_HEALTH_KEY_PREFIX

router = APIRouter()


@router.get("")
async def list_models(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    models = await get_all_current_pricing(db, redis)

    # Overlay the live health probe results onto the DB-cached status. Missing
    # Redis key (probe never ran, key expired) → leave DB status untouched.
    # Only explicit "unavailable" downgrades the response — "available" doesn't
    # need to overwrite since the DB default is already 'available'.
    if models:
        health_keys = [
            f"{MODEL_HEALTH_KEY_PREFIX}{model['id']}" for model in models
        ]
        health_values = await redis.mget(*health_keys)
        for model, health in zip(models, health_values):
            if health == "unavailable":
                model["status"] = "unavailable"

    return {"models": models}
