"""Admin storage stats: how much DB space text, images, and files consume.

Image and file sizes are derived from base64 length × 0.75 (decoded byte
estimate) rather than pg_column_size, so the figure reflects payload size
independent of TOAST compression.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_admin
from app.models.chat_file import ChatFile
from app.models.chat_image import ChatImage
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User

router = APIRouter()


class StorageStats(BaseModel):
    text_size_bytes: int
    images_size_bytes: int
    files_size_bytes: int
    total_images_count: int
    total_files_count: int
    total_conversations_count: int
    total_messages_count: int


@router.get("", response_model=StorageStats)
async def get_storage_stats(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> StorageStats:
    text_size_bytes = int(
        (
            await db.execute(
                select(
                    func.coalesce(
                        func.sum(func.pg_column_size(Message.content)), 0
                    )
                )
            )
        ).scalar()
        or 0
    )

    # base64 chars → decoded bytes (×0.75) for both stored copies.
    images_size_bytes = int(
        (
            await db.execute(
                select(
                    func.coalesce(
                        func.sum(
                            func.length(ChatImage.thumbnail_base64)
                            + func.length(ChatImage.full_base64)
                        ),
                        0,
                    )
                    * 0.75
                )
            )
        ).scalar()
        or 0
    )

    # base64 chars → decoded bytes (×0.75) for the stored payload.
    files_size_bytes = int(
        (
            await db.execute(
                select(
                    func.coalesce(
                        func.sum(func.length(ChatFile.base64_data)), 0
                    )
                    * 0.75
                )
            )
        ).scalar()
        or 0
    )

    total_images_count = int(
        (await db.execute(select(func.count()).select_from(ChatImage))).scalar() or 0
    )
    total_files_count = int(
        (await db.execute(select(func.count()).select_from(ChatFile))).scalar() or 0
    )
    total_conversations_count = int(
        (
            await db.execute(select(func.count()).select_from(Conversation))
        ).scalar()
        or 0
    )
    total_messages_count = int(
        (await db.execute(select(func.count()).select_from(Message))).scalar() or 0
    )

    return StorageStats(
        text_size_bytes=text_size_bytes,
        images_size_bytes=images_size_bytes,
        files_size_bytes=files_size_bytes,
        total_images_count=total_images_count,
        total_files_count=total_files_count,
        total_conversations_count=total_conversations_count,
        total_messages_count=total_messages_count,
    )
