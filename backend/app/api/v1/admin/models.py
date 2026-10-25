"""Admin model-catalogue endpoints.

POST /api/v1/admin/models/sync — manually trigger an OpenRouter model sync.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_redis, require_admin
from app.models.user import User
from app.services.model_sync_service import ModelSyncService

router = APIRouter()


class SyncResultResponse(BaseModel):
    added: int
    updated: int
    deactivated: int
    errors: list[str]


@router.post("/sync", response_model=SyncResultResponse)
async def sync_models(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> SyncResultResponse:
    result = await ModelSyncService().sync_models(db, redis)
    return SyncResultResponse(
        added=result.added,
        updated=result.updated,
        deactivated=result.deactivated,
        errors=result.errors,
    )
