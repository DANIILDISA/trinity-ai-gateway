"""Tests for the admin export + settings endpoints (Task 8.6)."""
from __future__ import annotations

import json
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import delete as sa_delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AdminAuditLog
from app.models.system_setting import SystemSetting
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
EXPORT_TX_URL = "/api/v1/admin/export/transactions"
EXPORT_SUMMARY_URL = "/api/v1/admin/export/summary"
EXPORT_PER_USER_URL = "/api/v1/admin/export/per-user"
SETTINGS_URL = "/api/v1/admin/settings"


_TEST_EMAILS = [
    "t86_admin@example.com",
    "t86_user_a@example.com",
    "t86_user_b@example.com",
    "t86_user_adj@example.com",
]


@pytest_asyncio.fixture(autouse=True)
async def clean_state(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    # Wipe my test users + their transactions + their usage_logs + their audit.
    for email in _TEST_EMAILS:
        existing = (
            await db_session.execute(
                select(User).where(User.email_lower == email.lower())
            )
        ).scalar_one_or_none()
        if existing is not None:
            await db_session.execute(
                sa_delete(UsageLog).where(UsageLog.user_id == existing.id)
            )
            await db_session.execute(
                sa_delete(Transaction).where(Transaction.user_id == existing.id)
            )
            await db_session.execute(
                sa_delete(Transaction).where(
                    Transaction.admin_user_id == existing.id
                )
            )
            await db_session.execute(
                sa_delete(AdminAuditLog).where(
                    AdminAuditLog.actor_id == existing.id
                )
            )
            await db_session.execute(
                text(
                    "UPDATE system_settings SET updated_by = NULL "
                    "WHERE updated_by = :uid"
                ),
                {"uid": existing.id},
            )
            await db_session.delete(existing)

    # Reset settings touched by tests so they don't leak.
    await db_session.execute(
        text(
            "UPDATE system_settings SET value = :v WHERE key = 'rate_limit_per_minute'"
        ),
        {"v": json.dumps(10)},
    )
    await db_session.commit()
    await redis_client.delete("settings:rate_limit_per_minute")
    await redis_client.delete("rate_limits:cache")


async def _register(http: AsyncClient, email: str) -> tuple[str, str]:
    r = await http.post(
        REGISTER_URL,
        json={"email": email, "password": "SecurePass1", "display_name": "Имя"},
    )
    assert r.status_code == 200, r.text
    d = r.json()
    return d["access_token"], d["user"]["id"]


async def _register_admin(
    http: AsyncClient, db: AsyncSession, email: str
) -> tuple[str, str]:
    token, uid = await _register(http, email)
    user = (
        await db.execute(select(User).where(User.id == _uuid.UUID(uid)))
    ).scalar_one()
    user.role = "admin"
    await db.commit()
    login = await http.post(
        LOGIN_URL, json={"email": email, "password": "SecurePass1"}
    )
    assert login.status_code == 200
    return login.json()["access_token"], uid


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_debit(
    db: AsyncSession, user_id: _uuid.UUID, when: datetime, amount_micro_rub: int
) -> None:
    db.add(
        Transaction(
            user_id=user_id,
            type="debit",
            amount_micro_rub=-abs(amount_micro_rub),
            balance_after_micro_rub=0,
            description="test debit",
            request_id=_uuid.uuid4(),
            idempotency_key=f"t:{_uuid.uuid4()}",
            created_at=when,
        )
    )


async def _seed_credit(
    db: AsyncSession, user_id: _uuid.UUID, when: datetime, amount_micro_rub: int
) -> None:
    db.add(
        Transaction(
            user_id=user_id,
            type="credit",
            amount_micro_rub=abs(amount_micro_rub),
            balance_after_micro_rub=abs(amount_micro_rub),
            description="test topup",
            idempotency_key=f"c:{_uuid.uuid4()}",
            created_at=when,
        )
    )


async def _seed_adjustment(
    db: AsyncSession, user_id: _uuid.UUID, when: datetime, amount_micro_rub: int
) -> None:
    # Admin balance adjustment (signed). Requires admin_reason.
    db.add(
        Transaction(
            user_id=user_id,
            type="adjustment",
            amount_micro_rub=amount_micro_rub,
            balance_after_micro_rub=0,
            description="test adjustment",
            admin_reason="qa ledger test",
            idempotency_key=f"a:{_uuid.uuid4()}",
            created_at=when,
        )
    )


async def _seed_usage_log(
    db: AsyncSession,
    *,
    user_id: _uuid.UUID,
    when: datetime,
    actual_cost_micro_rub: int,
    openrouter_cost_micro_usd: int,
    overdraft_absorbed_micro_rub: int = 0,
    input_tokens: int = 50,
    output_tokens: int = 100,
    status: str = "completed",
) -> None:
    db.add(
        UsageLog(
            user_id=user_id,
            request_id=_uuid.uuid4(),
            model_id="anthropic/claude-sonnet-4-6",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_input_tokens=input_tokens,
            estimated_output_tokens=output_tokens,
            estimated_cost_micro_rub=actual_cost_micro_rub,
            actual_cost_micro_rub=actual_cost_micro_rub,
            reserved_micro_rub=actual_cost_micro_rub * 2,
            overdraft_absorbed_micro_rub=overdraft_absorbed_micro_rub,
            openrouter_cost_micro_usd=openrouter_cost_micro_usd,
            exchange_rate_micro_rub=82_500_000,
            status=status,
            created_at=when,
        )
    )


def _today() -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    return yesterday.isoformat(), today.isoformat()


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_export_transactions_csv(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "t86_admin@example.com"
    )
    _, uid = await _register(http_client, "t86_user_a@example.com")
    when = datetime.now(timezone.utc) - timedelta(hours=1)
    await _seed_credit(db_session, _uuid.UUID(uid), when, 500_000_000)
    await _seed_debit(db_session, _uuid.UUID(uid), when, 1_500_000)
    await db_session.commit()

    date_from, date_to = _today()
    resp = await http_client.post(
        EXPORT_TX_URL,
        headers=_auth(admin_token),
        json={"date_from": date_from, "date_to": date_to},
    )
    assert resp.status_code == 200, resp.text
    body = resp.content
    # BOM present (utf-8-sig).
    assert body[:3] == b"\xef\xbb\xbf"
    # Content-Disposition has the date range.
    disp = resp.headers.get("content-disposition", "")
    assert f"transactions_{date_from}_{date_to}.csv" in disp

    # Header + at least our 2 rows (other tests may add more).
    text_csv = body.decode("utf-8-sig")
    lines = [ln for ln in text_csv.splitlines() if ln.strip()]
    assert lines[0].startswith("Дата,Тип,")
    # Filter to our user's rows by email.
    ours = [ln for ln in lines[1:] if "t86_user_a@example.com" in ln]
    assert len(ours) == 2


async def test_export_summary_margin(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "t86_admin@example.com"
    )
    _, uid = await _register(http_client, "t86_user_a@example.com")
    when = datetime.now(timezone.utc) - timedelta(hours=1)

    # Wipe any leftover usage_logs / transactions so the summary reflects only ours.
    await db_session.execute(sa_delete(UsageLog))
    await db_session.execute(
        sa_delete(Transaction).where(Transaction.type.in_(("debit", "credit")))
    )

    await _seed_debit(db_session, _uuid.UUID(uid), when, 3_000_000)
    await _seed_usage_log(
        db_session,
        user_id=_uuid.UUID(uid),
        when=when,
        actual_cost_micro_rub=3_000_000,
        openrouter_cost_micro_usd=20_000,
    )
    await db_session.commit()

    date_from, date_to = _today()
    resp = await http_client.post(
        EXPORT_SUMMARY_URL,
        headers=_auth(admin_token),
        json={"date_from": date_from, "date_to": date_to},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["revenue"]["total_micro_rub"] == 3_000_000
    assert data["revenue"]["total_display"] == "3.0000"
    # cost = 20_000 µUSD × 82_500_000 µRUB / 10^6 = 1_650_000 µ₽
    # margin = (3_000_000 - 1_650_000) / 3_000_000 × 100 = 45%
    assert 44.0 < data["margin"]["percent"] < 46.0
    assert data["requests"]["completed"] >= 1
    assert data["users"]["unique_payers"] == 0  # only debits here


async def test_export_per_user_csv(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "t86_admin@example.com"
    )
    _, uid_a = await _register(http_client, "t86_user_a@example.com")
    _, uid_b = await _register(http_client, "t86_user_b@example.com")

    when = datetime.now(timezone.utc) - timedelta(hours=1)
    await _seed_credit(db_session, _uuid.UUID(uid_a), when, 500_000_000)
    await _seed_credit(db_session, _uuid.UUID(uid_b), when, 100_000_000)
    await db_session.commit()

    date_from, date_to = _today()
    resp = await http_client.post(
        EXPORT_PER_USER_URL,
        headers=_auth(admin_token),
        json={"date_from": date_from, "date_to": date_to},
    )
    assert resp.status_code == 200, resp.text
    body = resp.content
    assert body[:3] == b"\xef\xbb\xbf"
    text_csv = body.decode("utf-8-sig")
    lines = [ln for ln in text_csv.splitlines() if ln.strip()]
    assert lines[0].startswith("Email,Имя,")
    # Both our users should appear (others may too).
    assert any("t86_user_a@example.com" in ln for ln in lines[1:])
    assert any("t86_user_b@example.com" in ln for ln in lines[1:])


async def test_export_per_user_includes_adjustments(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    """tr-ej0w: per-user CSV 'Пополнено' must include admin adjustments/refunds
    (not just type='credit'), and a user whose ONLY transaction is an adjustment
    must still appear (the HAVING filter dropped adjustment-only users)."""
    import csv as _csv
    import io as _io

    admin_token, _ = await _register_admin(
        http_client, db_session, "t86_admin@example.com"
    )
    _, uid = await _register(http_client, "t86_user_adj@example.com")
    when = datetime.now(timezone.utc) - timedelta(hours=1)
    # ONLY an admin credit adjustment (+300 ₽) — no 'credit'/'debit' rows.
    await _seed_adjustment(db_session, _uuid.UUID(uid), when, 300_000_000)
    await db_session.commit()

    date_from, date_to = _today()
    resp = await http_client.post(
        EXPORT_PER_USER_URL,
        headers=_auth(admin_token),
        json={"date_from": date_from, "date_to": date_to},
    )
    assert resp.status_code == 200, resp.text
    reader = list(_csv.reader(_io.StringIO(resp.content.decode("utf-8-sig"))))
    header = reader[0]
    topped_idx = header.index("Пополнено (₽)")
    row = next(
        (r for r in reader[1:] if r and r[0] == "t86_user_adj@example.com"),
        None,
    )
    assert row is not None, "adjustment-only user missing from per-user export"
    assert row[topped_idx] == "300.0000"


async def test_get_settings(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "t86_admin@example.com"
    )
    resp = await http_client.get(SETTINGS_URL, headers=_auth(admin_token))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "rate_limits" in data
    assert "billing" in data
    assert "payments" in data
    assert "openrouter" in data
    assert "reconciliation" in data
    # 9+ keys total.
    total_keys = sum(len(v) for v in data.values())
    assert total_keys >= 9
    # Types: rate_limit_per_minute is int.
    assert isinstance(data["rate_limits"]["rate_limit_per_minute"], int)
    # min_topup_amount_rub == 500 from seed.
    assert data["payments"]["min_topup_amount_rub"] == 500


async def test_update_setting(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "t86_admin@example.com"
    )
    resp = await http_client.put(
        f"{SETTINGS_URL}/rate_limit_per_minute",
        headers=_auth(admin_token),
        json={"value": 15},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == 15

    # Persisted.
    raw = (
        await db_session.execute(
            select(SystemSetting.value).where(
                SystemSetting.key == "rate_limit_per_minute"
            )
        )
    ).scalar_one()
    assert int(raw) == 15

    audit = (
        await db_session.execute(
            select(AdminAuditLog).where(
                AdminAuditLog.action == "admin.settings.update",
                AdminAuditLog.target_id == "rate_limit_per_minute",
            )
        )
    ).scalars().all()
    assert len(audit) >= 1
    assert audit[-1].details["key"] == "rate_limit_per_minute"
    assert audit[-1].details["new_value"] == 15


async def test_update_setting_invalid_key(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "t86_admin@example.com"
    )
    resp = await http_client.put(
        f"{SETTINGS_URL}/totally_made_up_key",
        headers=_auth(admin_token),
        json={"value": 42},
    )
    assert resp.status_code == 400, resp.text
