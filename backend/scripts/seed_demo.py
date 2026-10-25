#!/usr/bin/env python3
"""Demo seed script — creates 3 test users with balance for manual testing.

Usage:
    docker compose exec backend python scripts/seed_demo.py

Safe to run multiple times: user creation is guarded by email lookup, balance
credit goes through credit_balance() which uses ON CONFLICT DO NOTHING on
the per-user idempotency key.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from sqlalchemy import select

from app.config import settings
from app.core.constants import micro_to_display_balance
from app.core.database import dispose_engine, get_session_factory, init_engine
from app.core.security import hash_password
from app.models.user import User
from app.services.billing_service import credit_balance

DEMO_BALANCE_MICRO_RUB = 500_000_000  # 500 ₽
DEMO_PASSWORD = "demo123"


@dataclass(frozen=True)
class DemoUser:
    email: str
    display_name: str


DEMO_USERS: tuple[DemoUser, ...] = (
    DemoUser(email="test1@demo.ru", display_name="Тест Пользователь 1"),
    DemoUser(email="test2@demo.ru", display_name="Тест Пользователь 2"),
    DemoUser(email="test3@demo.ru", display_name="Тест Пользователь 3"),
)


async def _seed_user(session_factory, demo: DemoUser) -> None:
    async with session_factory() as db:
        # 1. Find or create the user.
        existing = (
            await db.execute(
                select(User).where(User.email_lower == demo.email.lower())
            )
        ).scalar_one_or_none()

        if existing is None:
            user = User(
                email=demo.email,
                display_name=demo.display_name,
                password_hash=await hash_password(DEMO_PASSWORD),
                role="user",
                status="active",
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            created = True
        else:
            user = existing
            created = False

        # 2. Top up to ≥ 500 ₽ if we're below that.
        if user.balance_micro_rub < DEMO_BALANCE_MICRO_RUB:
            to_credit = DEMO_BALANCE_MICRO_RUB - user.balance_micro_rub
            await credit_balance(
                db,
                user_id=user.id,
                amount_micro_rub=to_credit,
                idempotency_key=f"seed-demo-{demo.email}",
                description="Demo seed balance",
            )
            await db.commit()
            await db.refresh(user)

        action = "создан" if created else "уже существует"
        balance_display = micro_to_display_balance(user.balance_micro_rub)
        print(f"✓ {demo.email} — {action}, баланс {balance_display} ₽")


async def main() -> None:
    init_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )
    try:
        session_factory = get_session_factory()
        for demo in DEMO_USERS:
            await _seed_user(session_factory, demo)
    finally:
        await dispose_engine()

    print("=== Seed complete ===")
    print(f"Войдите как {DEMO_USERS[0].email} / {DEMO_PASSWORD}")
    print("Или откройте /admin для управления пользователями")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
