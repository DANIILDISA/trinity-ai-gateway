import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import encrypt_key
from app.dependencies import get_db, require_admin
from app.models.api_key import ApiKey
from app.models.audit_log import AdminAuditLog
from app.models.user import User

router = APIRouter()


class KeyView(BaseModel):
    id: uuid.UUID
    name: str
    label: str
    key_suffix: str
    status: str
    is_primary: bool
    account_name: str | None
    account_blocked: bool
    total_requests: int
    total_cost_micro_usd: int
    last_used_at: datetime | None
    created_at: datetime


class AddKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    key: str = Field(min_length=8)
    account_name: str | None = Field(default=None, max_length=100)


class PatchKeyRequest(BaseModel):
    status: str | None = Field(default=None, pattern=r"^(active|disabled|exhausted)$")
    account_name: str | None = Field(default=None, max_length=100)
    account_blocked: bool | None = None


class BlockAccountResponse(BaseModel):
    affected_keys: int


def _to_view(row: ApiKey) -> KeyView:
    return KeyView(
        id=row.id,
        # `name` retained for back-compat with earlier test assertions.
        name=row.label,
        label=row.label,
        key_suffix=row.key_suffix,
        status=row.status,
        is_primary=row.is_primary,
        account_name=row.account_name,
        account_blocked=row.account_blocked,
        total_requests=row.total_requests,
        total_cost_micro_usd=row.total_cost_micro_usd,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
    )


def _audit(
    db: AsyncSession,
    admin: User,
    action: str,
    key_id: uuid.UUID,
    details: dict[str, Any] | None = None,
) -> None:
    db.add(
        AdminAuditLog(
            actor_id=admin.id,
            actor_email=admin.email,
            action=action,
            target_type="api_key",
            target_id=str(key_id),
            details=details,
        )
    )


@router.get("", response_model=list[KeyView])
async def list_keys(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[KeyView]:
    rows = (
        await db.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))
    ).scalars().all()
    return [_to_view(r) for r in rows]


@router.post("", response_model=KeyView)
async def add_key(
    body: AddKeyRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> KeyView:
    row = ApiKey(
        label=body.name,
        encrypted_key=encrypt_key(body.key),
        key_suffix=body.key[-8:],
        status="active",
        is_primary=False,
        account_name=body.account_name,
    )
    db.add(row)
    await db.flush()
    _audit(
        db,
        admin,
        "admin.key.add",
        row.id,
        details={
            "label": body.name,
            "key_suffix": row.key_suffix,
            "account_name": body.account_name,
        },
    )
    await db.commit()
    await db.refresh(row)
    return _to_view(row)


@router.patch("/{key_id}", response_model=KeyView)
async def patch_key(
    key_id: uuid.UUID,
    body: PatchKeyRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> KeyView:
    row = (
        await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Ключ не найден")

    changes: dict[str, Any] = {}
    if body.status is not None and body.status != row.status:
        changes["status"] = {"from": row.status, "to": body.status}
        row.status = body.status
    if body.account_name is not None and body.account_name != row.account_name:
        changes["account_name"] = {"from": row.account_name, "to": body.account_name}
        row.account_name = body.account_name
    if body.account_blocked is not None and body.account_blocked != row.account_blocked:
        changes["account_blocked"] = {
            "from": row.account_blocked,
            "to": body.account_blocked,
        }
        row.account_blocked = body.account_blocked

    _audit(db, admin, "admin.key.update", key_id, details=changes)
    await db.commit()
    await db.refresh(row)
    return _to_view(row)


async def _set_account_blocked(
    db: AsyncSession,
    admin: User,
    account_name: str,
    blocked: bool,
) -> int:
    """Flip account_blocked for every key with this account_name. Returns the
    number of rows affected; raises 404 when the account has no keys."""
    rows = (
        await db.execute(
            select(ApiKey).where(ApiKey.account_name == account_name)
        )
    ).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")

    for row in rows:
        row.account_blocked = blocked
        _audit(
            db,
            admin,
            "admin.key.account_block" if blocked else "admin.key.account_unblock",
            row.id,
            details={"account_name": account_name, "account_blocked": blocked},
        )
    await db.commit()
    return len(rows)


@router.post(
    "/accounts/{account_name}/block", response_model=BlockAccountResponse
)
async def block_account(
    account_name: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> BlockAccountResponse:
    affected = await _set_account_blocked(db, admin, account_name, True)
    return BlockAccountResponse(affected_keys=affected)


@router.post(
    "/accounts/{account_name}/unblock", response_model=BlockAccountResponse
)
async def unblock_account(
    account_name: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> BlockAccountResponse:
    affected = await _set_account_blocked(db, admin, account_name, False)
    return BlockAccountResponse(affected_keys=affected)


@router.post("/{key_id}/activate", response_model=KeyView)
async def activate_key(
    key_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> KeyView:
    row = (
        await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Ключ не найден")

    # Clear primary flag on every other key first.
    await db.execute(
        update(ApiKey).where(ApiKey.id != key_id).values(is_primary=False)
    )
    row.is_primary = True
    row.status = "active"
    _audit(db, admin, "admin.key.activate", key_id)
    await db.commit()
    await db.refresh(row)
    return _to_view(row)
