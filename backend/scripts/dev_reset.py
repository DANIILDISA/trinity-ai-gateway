"""One-shot dev environment reset — wipes user-generated data, re-seeds users,
fires real Telegram + email notifications, and reports outcomes as JSON.

Run inside the backend container:
    docker compose exec -T backend python scripts/dev_reset.py

NOT for production use. Idempotent only in the sense that re-running just
re-wipes and re-seeds.
"""
from __future__ import annotations

import asyncio
import os
import json
import logging
import sys

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.database import dispose_engine, get_session_factory, init_engine
from app.core.security import hash_password
from app.models.user import User
from app.services import email_service as es
from app.services.billing_service import credit_balance

logging.basicConfig(level=logging.WARNING)

ADMIN_EMAIL = os.environ.get("SEED_ADMIN_EMAIL", "admin@example.com")
ADMIN_DISPLAY_NAME = "Admin"

TEST_USERS = [
    ("test1@demo.ru", "Тест Пользователь 1"),
    ("test2@demo.ru", "Тест Пользователь 2"),
    ("test3@demo.ru", "Тест Пользователь 3"),
]
TEST_USER_PASSWORD = "demo123"
TEST_USER_BALANCE_MICRO_RUB = 100_000_000  # 100 ₽

# FK-safe wipe order. usage_logs holds api_keys FK → must precede api_keys.
# system_settings is preserved (per task spec), but its updated_by FK is nulled
# so the users delete doesn't violate it.
WIPE_TABLES = [
    "usage_logs",
    "reservations",
    "transactions",
    "notifications",
    "alerts",
    "messages",
    "conversations",
    "api_keys",
    "refresh_tokens",
    "password_reset_tokens",
    "admin_audit_log",
]


async def capture_admin_hash(db: AsyncSession) -> str | None:
    row = await db.execute(
        text("SELECT password_hash FROM users WHERE email_lower=:e"),
        {"e": ADMIN_EMAIL.lower()},
    )
    return row.scalar_one_or_none()


async def wipe_user_data(db: AsyncSession) -> dict[str, int]:
    counts: dict[str, int] = {}
    for tbl in WIPE_TABLES:
        result = await db.execute(text(f"DELETE FROM {tbl}"))
        counts[tbl] = result.rowcount or 0
    # Null out system_settings.updated_by so deleting users doesn't violate FK.
    await db.execute(text("UPDATE system_settings SET updated_by = NULL"))
    users_result = await db.execute(text("DELETE FROM users"))
    counts["users"] = users_result.rowcount or 0
    await db.commit()
    return counts


async def create_admin(db: AsyncSession, password_hash: str | None) -> User:
    admin = User(
        email=ADMIN_EMAIL,
        display_name=ADMIN_DISPLAY_NAME,
        password_hash=password_hash,
        role="admin",
        status="active",
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)
    return admin


async def create_test_user(
    db: AsyncSession, email: str, display_name: str
) -> User:
    user = User(
        email=email,
        display_name=display_name,
        password_hash=await hash_password(TEST_USER_PASSWORD),
        role="user",
        status="active",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    await credit_balance(
        db,
        user_id=user.id,
        amount_micro_rub=TEST_USER_BALANCE_MICRO_RUB,
        idempotency_key=f"dev-reset-seed-{email}",
        description="Dev reset starting balance",
    )
    await db.commit()
    await db.refresh(user)
    return user


# ── Telegram ────────────────────────────────────────────────────────────────


_SEVERITY_PREFIX = {"info": "🔵", "warning": "🟡", "critical": "🔴"}


async def send_telegram(text_body: str, severity: str) -> dict:
    """Post directly to Telegram Bot API. Bypasses demo_mode and the
    info-filtering in telegram_service so all three severities reach the API."""
    prefix = _SEVERITY_PREFIX[severity]
    full = f"{prefix} {severity.upper()}: {text_body}"
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            url, json={"chat_id": settings.telegram_chat_id, "text": full}
        )
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:300]}
    return {
        "severity": severity,
        "status_code": resp.status_code,
        "ok": resp.is_success and body.get("ok") is True,
        "message_id": (body.get("result") or {}).get("message_id"),
        "error": None if resp.is_success else body,
    }


# ── Email ───────────────────────────────────────────────────────────────────


async def send_email(template_name: str, subject: str, html: str, text_body: str) -> dict:
    """Synchronously POST to Resend so we can capture the message_id."""
    payload = {
        "from": settings.resend_from_email,
        "to": [ADMIN_EMAIL],
        "subject": subject,
        "text": text_body,
        "html": html,
    }
    headers = {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://api.resend.com/emails", json=payload, headers=headers
        )
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:300]}
    return {
        "template": template_name,
        "status_code": resp.status_code,
        "ok": resp.is_success,
        "message_id": body.get("id") if resp.is_success else None,
        "error": None if resp.is_success else body,
    }


def _render_pair(base: str, **ctx) -> tuple[str, str]:
    """Render <base>.html and <base>.txt and return (html, text)."""
    html = es._env.get_template(f"{base}.html").render(**ctx)
    text = es._env.get_template(f"{base}.txt").render(**ctx)
    return html, text


# ── Orchestration ───────────────────────────────────────────────────────────


async def main() -> int:
    init_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )
    factory = get_session_factory()
    report: dict = {}

    try:
        # 1. Capture admin password hash before wipe.
        async with factory() as db:
            admin_hash = await capture_admin_hash(db)
            report["admin_hash_captured"] = admin_hash is not None

        # 2. Wipe.
        async with factory() as db:
            report["wiped"] = await wipe_user_data(db)

        # 3. Re-create admin (preserved hash).
        async with factory() as db:
            admin = await create_admin(db, admin_hash)
            report["admin"] = {
                "email": admin.email,
                "id": str(admin.id),
                "role": admin.role,
                "status": admin.status,
                "password_preserved": admin_hash is not None,
            }

        # 4. Re-create test users + balance.
        async with factory() as db:
            report["test_users"] = []
            for email, display_name in TEST_USERS:
                u = await create_test_user(db, email, display_name)
                report["test_users"].append(
                    {
                        "email": u.email,
                        "id": str(u.id),
                        "balance_micro_rub": u.balance_micro_rub,
                        "password": TEST_USER_PASSWORD,
                    }
                )

        # 5. Telegram.
        report["telegram"] = []
        report["telegram"].append(
            await send_telegram(
                "🔵 Dev reset complete — система перезапущена", "info"
            )
        )
        report["telegram"].append(
            await send_telegram("Test warning alert after dev reset", "warning")
        )
        report["telegram"].append(
            await send_telegram("Test critical alert after dev reset", "critical")
        )

        # 6. Emails — one per template.
        report["emails"] = []

        html, txt = _render_pair(
            "password_reset", reset_link="https://trinity.local/reset/test"
        )
        report["emails"].append(
            await send_email(
                "password_reset", "Сброс пароля Trinity", html, txt
            )
        )

        html, txt = _render_pair(
            "low_balance",
            remaining_requests=3,
            topup_link="https://trinity.local/balance",
        )
        report["emails"].append(
            await send_email("low_balance", "Низкий баланс Trinity", html, txt)
        )

        html, txt = _render_pair(
            "topup_confirmed", amount=500, new_balance=500
        )
        report["emails"].append(
            await send_email(
                "topup_confirmed", "Пополнение баланса Trinity", html, txt
            )
        )

        html, txt = _render_pair(
            "tariff_change", old_tariff="Стандарт", new_tariff="Премиум"
        )
        report["emails"].append(
            await send_email(
                "tariff_change", "Изменение тарифов Trinity", html, txt
            )
        )

    finally:
        await dispose_engine()

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    # Exit non-zero if any side-effect failed.
    if not all(t["ok"] for t in report.get("telegram", [])):
        return 1
    if not all(e["ok"] for e in report.get("emails", [])):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
