#!/usr/bin/env python3
"""Reset the DEV database (trinity) to a clean, known state.

Wipes all user-generated data and creates a single admin user. Schema and
migration-seeded reference data are preserved.

Usage (one-shot compose service):
    docker compose --profile seed run --rm seed
Or directly inside the running backend container:
    docker compose exec backend python scripts/seed_dev.py

Connects to DATABASE_URL (the dev `trinity` DB) — NOT the test DB. NOT for
production use.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Ensure the backend root (which contains the `app` package) is importable when
# this file is run directly as `python scripts/seed_dev.py` — running a script
# puts the script's own dir on sys.path, not the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.database import dispose_engine, get_session_factory, init_engine
from app.core.security import hash_password
from app.models.transaction import Transaction
from app.models.user import User

ADMIN_EMAIL = os.environ.get("SEED_ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("SEED_ADMIN_PASSWORD", "change-me-admin")
ADMIN_DISPLAY_NAME = "Admin"
ADMIN_BALANCE_MICRO_RUB = 100_000_000  # 100.00 ₽

# User-data tables wiped in FK-safe order (children before parents).
# Preserved on purpose:
#   - alembic_version : migration bookkeeping, must never be cleared
#   - system_settings : migration-seeded config (001); its updated_by FK to
#                       users is nulled below so the users DELETE doesn't fail
#   - model_pricing   : migration-seeded reference data (002). Wiping it would
#                       leave dev with no models until a live OpenRouter sync,
#                       so it is kept even though it is not user data.
_WIPE_TABLES: tuple[str, ...] = (
    "usage_logs",
    "messages",
    "conversations",
    "reservations",
    "transactions",
    "notifications",
    "alerts",
    "admin_audit_log",
    "refresh_tokens",
    "password_reset_tokens",
    "api_keys",
    "users",
)


async def _wipe_user_data(db: AsyncSession) -> None:
    # Null the system_settings -> users FK so deleting users can't violate it.
    await db.execute(text("UPDATE system_settings SET updated_by = NULL"))
    for table in _WIPE_TABLES:
        await db.execute(text(f"DELETE FROM {table}"))
    await db.commit()


async def _create_admin(db: AsyncSession) -> User:
    admin = User(
        email=ADMIN_EMAIL,
        display_name=ADMIN_DISPLAY_NAME,
        password_hash=await hash_password(ADMIN_PASSWORD),
        role="admin",
        status="active",
        balance_micro_rub=ADMIN_BALANCE_MICRO_RUB,
    )
    db.add(admin)
    await db.flush()  # populate admin.id for the backing transaction

    # Back the seeded balance with a matching ledger entry so reconciliation
    # sees balance == sum(transactions) instead of a 100% discrepancy. The
    # transaction_type enum has no 'deposit' value — an incoming top-up is a
    # 'credit' (and the chk_credit_positive constraint requires amount > 0).
    db.add(
        Transaction(
            user_id=admin.id,
            type="credit",
            amount_micro_rub=ADMIN_BALANCE_MICRO_RUB,
            balance_after_micro_rub=ADMIN_BALANCE_MICRO_RUB,
            description="Начальный баланс (seed)",
            idempotency_key="seed_admin_deposit",
        )
    )

    await db.commit()
    await db.refresh(admin)
    return admin


async def main() -> int:
    # Guard: never run the dev wipe against the test DB by mistake.
    db_name = settings.database_url.rsplit("/", 1)[-1]
    if db_name != "trinity":
        print(
            f"Refusing to seed: DATABASE_URL points at {db_name!r}, expected "
            "'trinity'. This script only resets the dev database.",
            file=sys.stderr,
        )
        return 1

    init_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )
    try:
        factory = get_session_factory()
        async with factory() as db:
            await _wipe_user_data(db)
            # Sanity check: the wipe + recreate must leave exactly one admin.
            existing = (
                await db.execute(select(User).where(User.email_lower == ADMIN_EMAIL.lower()))
            ).scalar_one_or_none()
            if existing is None:
                await _create_admin(db)
    finally:
        await dispose_engine()

    print(f"Dev database seeded. Admin: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
