"""Tests for the admin user management endpoints (Task 8.3)."""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AdminAuditLog
from app.models.notification import Notification
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
LIST_URL = "/api/v1/admin/users"


def _detail_url(uid: str) -> str:
    return f"/api/v1/admin/users/{uid}"


def _block_url(uid: str) -> str:
    return f"/api/v1/admin/users/{uid}/block"


def _unblock_url(uid: str) -> str:
    return f"/api/v1/admin/users/{uid}/unblock"


def _adjust_url(uid: str) -> str:
    return f"/api/v1/admin/users/{uid}/adjust-balance"


_TEST_EMAILS = [
    "u83_admin@example.com",
    "u83_target_a@example.com",
    "u83_target_b@example.com",
    "u83_target_stats@example.com",
    "u83_target_avg@example.com",
    "u83_target_block@example.com",
    "u83_target_unblock@example.com",
    "u83_target_credit@example.com",
    "u83_target_reason@example.com",
    "u83_target_ledger@example.com",
    "u83_search_alice@example.com",
    "u83_search_bob@example.com",
]


@pytest_asyncio.fixture(autouse=True)
async def clean_state(db_session: AsyncSession) -> None:
    for email in _TEST_EMAILS:
        existing = (
            await db_session.execute(
                select(User).where(User.email_lower == email.lower())
            )
        ).scalar_one_or_none()
        if existing is not None:
            await db_session.execute(
                sa_delete(Notification).where(Notification.user_id == existing.id)
            )
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
                sa_delete(AdminAuditLog).where(
                    AdminAuditLog.target_id == str(existing.id)
                )
            )
            await db_session.delete(existing)
    await db_session.commit()


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


async def _seed_usage_log(
    db: AsyncSession,
    *,
    user_id: _uuid.UUID,
    model_id: str = "anthropic/claude-sonnet-4-6",
    when: datetime,
    actual_cost_micro_rub: int,
    input_tokens: int = 100,
    output_tokens: int = 200,
    status: str = "completed",
) -> None:
    db.add(
        UsageLog(
            user_id=user_id,
            request_id=_uuid.uuid4(),
            model_id=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_input_tokens=input_tokens,
            estimated_output_tokens=output_tokens,
            estimated_cost_micro_rub=actual_cost_micro_rub,
            actual_cost_micro_rub=actual_cost_micro_rub,
            reserved_micro_rub=actual_cost_micro_rub * 2,
            overdraft_absorbed_micro_rub=0,
            openrouter_cost_micro_usd=10_000,
            exchange_rate_micro_rub=82_500_000,
            status=status,
            created_at=when,
        )
    )


async def _seed_debit_transaction(
    db: AsyncSession,
    *,
    user_id: _uuid.UUID,
    when: datetime,
    amount_micro_rub: int,
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


async def _seed_credit_transaction(
    db: AsyncSession,
    *,
    user_id: _uuid.UUID,
    when: datetime,
    amount_micro_rub: int,
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


async def _seed_refund_transaction(
    db: AsyncSession,
    *,
    user_id: _uuid.UUID,
    when: datetime,
    amount_micro_rub: int,
) -> None:
    db.add(
        Transaction(
            user_id=user_id,
            type="refund",
            amount_micro_rub=abs(amount_micro_rub),
            balance_after_micro_rub=abs(amount_micro_rub),
            description="test refund",
            idempotency_key=f"r:{_uuid.uuid4()}",
            created_at=when,
        )
    )


async def _seed_adjustment_transaction(
    db: AsyncSession,
    *,
    user_id: _uuid.UUID,
    when: datetime,
    amount_micro_rub: int,
) -> None:
    # Signed: positive = admin credit, negative = admin debit. `adjustment`
    # rows require admin_reason (CHECK chk_adjustment_has_reason).
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


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_search_by_email(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "u83_admin@example.com"
    )
    _, alice_id = await _register(http_client, "u83_search_alice@example.com")
    _, _bob_id = await _register(http_client, "u83_search_bob@example.com")

    resp = await http_client.get(
        LIST_URL,
        headers=_auth(admin_token),
        params={"search": "alice", "per_page": 50},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    found = [it for it in items if it["id"] == alice_id]
    assert len(found) == 1
    # Case-insensitive: search lowercase but stored case-preserved.
    assert "alice" in found[0]["email"].lower()


async def test_user_detail_stats(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "u83_admin@example.com"
    )
    _, target_id = await _register(http_client, "u83_target_stats@example.com")
    uid = _uuid.UUID(target_id)
    now = datetime.now(timezone.utc) - timedelta(hours=1)

    # Two requests, total cost 3 RUB; one credit of 500 RUB; debit 3 RUB.
    await _seed_usage_log(
        db_session, user_id=uid, when=now, actual_cost_micro_rub=1_000_000,
        input_tokens=50, output_tokens=100,
    )
    await _seed_usage_log(
        db_session, user_id=uid, when=now, actual_cost_micro_rub=2_000_000,
        input_tokens=70, output_tokens=130,
    )
    await _seed_credit_transaction(
        db_session, user_id=uid, when=now, amount_micro_rub=500_000_000
    )
    await _seed_debit_transaction(
        db_session, user_id=uid, when=now, amount_micro_rub=3_000_000
    )
    await db_session.commit()

    resp = await http_client.get(_detail_url(target_id), headers=_auth(admin_token))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    stats = data["stats"]
    assert stats["total_topped_up_micro_rub"] == 500_000_000
    assert stats["total_spent_micro_rub"] == 3_000_000
    assert stats["total_requests"] == 2
    assert stats["total_tokens"] == 50 + 100 + 70 + 130
    # AVG over completed: (1 + 2) / 2 = 1.5 RUB = 1_500_000 µ₽
    assert stats["avg_request_cost_micro_rub"] == 1_500_000
    assert stats["most_used_model"] == "anthropic/claude-sonnet-4-6"
    assert stats["requests_today"] == 2  # both seeded "1h ago" — same UTC day usually
    assert data["has_password"] is True
    assert data["has_yandex"] is False


async def test_user_detail_deposited_includes_adjustments_and_refunds(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    """tr-ej0w: 'Всего пополнено' must include ALL credit-like inflows
    (credit + refund + positive adjustment), and 'Всего потрачено' must include
    ALL outflows (debit + negative admin adjustment). Otherwise admin top-ups
    are invisible and deposited − spent ≠ balance (the '100 − 228.75 != 11.62'
    ledger break). Acceptance tr-8dsi: deposited − spent == net signed ledger.
    """
    admin_token, _ = await _register_admin(
        http_client, db_session, "u83_admin@example.com"
    )
    _, target_id = await _register(
        http_client, "u83_target_ledger@example.com"
    )
    uid = _uuid.UUID(target_id)
    now = datetime.now(timezone.utc) - timedelta(hours=1)

    await _seed_credit_transaction(
        db_session, user_id=uid, when=now, amount_micro_rub=500_000_000
    )  # top-up +500
    await _seed_refund_transaction(
        db_session, user_id=uid, when=now, amount_micro_rub=20_000_000
    )  # refund +20
    await _seed_adjustment_transaction(
        db_session, user_id=uid, when=now, amount_micro_rub=140_000_000
    )  # admin credit +140
    await _seed_adjustment_transaction(
        db_session, user_id=uid, when=now, amount_micro_rub=-10_000_000
    )  # admin debit -10
    await _seed_debit_transaction(
        db_session, user_id=uid, when=now, amount_micro_rub=3_000_000
    )  # spend -3
    await db_session.commit()

    resp = await http_client.get(
        _detail_url(target_id), headers=_auth(admin_token)
    )
    assert resp.status_code == 200, resp.text
    stats = resp.json()["stats"]

    # Deposited = ALL positive inflows: 500 + 20 + 140 = 660 RUB.
    assert stats["total_topped_up_micro_rub"] == 660_000_000
    # Spent = ALL outflows: debit 3 + admin debit 10 = 13 RUB.
    assert stats["total_spent_micro_rub"] == 13_000_000
    # Acceptance identity (tr-8dsi): deposited − spent == net signed ledger.
    assert (
        stats["total_topped_up_micro_rub"] - stats["total_spent_micro_rub"]
        == 500_000_000 + 20_000_000 + 140_000_000 - 10_000_000 - 3_000_000
    )


async def test_avg_cost_capped_at_100(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "u83_admin@example.com"
    )
    _, target_id = await _register(http_client, "u83_target_avg@example.com")
    uid = _uuid.UUID(target_id)
    base = datetime.now(timezone.utc) - timedelta(hours=2)

    # 50 oldest at 100 µ₽; 100 newest at 200 µ₽. Last 100 average should be 200.
    for i in range(50):
        await _seed_usage_log(
            db_session,
            user_id=uid,
            when=base + timedelta(seconds=i),
            actual_cost_micro_rub=100,
        )
    for i in range(100):
        await _seed_usage_log(
            db_session,
            user_id=uid,
            when=base + timedelta(minutes=10, seconds=i),
            actual_cost_micro_rub=200,
        )
    await db_session.commit()

    resp = await http_client.get(_detail_url(target_id), headers=_auth(admin_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["stats"]["avg_request_cost_micro_rub"] == 200
    assert resp.json()["stats"]["total_requests"] == 150


async def test_block_user(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, admin_id = await _register_admin(
        http_client, db_session, "u83_admin@example.com"
    )
    _, target_id = await _register(http_client, "u83_target_block@example.com")

    resp = await http_client.post(
        _block_url(target_id),
        headers=_auth(admin_token),
        json={"reason": "Подозрительная активность"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "blocked"

    user = (
        await db_session.execute(
            select(User).where(User.id == _uuid.UUID(target_id))
        )
    ).scalar_one()
    await db_session.refresh(user)
    assert user.status == "blocked"
    assert user.block_reason == "Подозрительная активность"

    audit = (
        await db_session.execute(
            select(AdminAuditLog).where(
                AdminAuditLog.action == "admin.user.block",
                AdminAuditLog.target_id == target_id,
            )
        )
    ).scalar_one()
    assert audit.actor_id == _uuid.UUID(admin_id)
    assert audit.details == {"reason": "Подозрительная активность"}


async def test_unblock_user(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "u83_admin@example.com"
    )
    _, target_id = await _register(http_client, "u83_target_unblock@example.com")

    block = await http_client.post(
        _block_url(target_id),
        headers=_auth(admin_token),
        json={"reason": "temp block for test"},
    )
    assert block.status_code == 200

    unblock = await http_client.post(
        _unblock_url(target_id), headers=_auth(admin_token)
    )
    assert unblock.status_code == 200, unblock.text
    assert unblock.json()["status"] == "active"

    user = (
        await db_session.execute(
            select(User).where(User.id == _uuid.UUID(target_id))
        )
    ).scalar_one()
    await db_session.refresh(user)
    assert user.status == "active"
    assert user.block_reason is None


async def test_adjust_balance_credit(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "u83_admin@example.com"
    )
    _, target_id = await _register(http_client, "u83_target_credit@example.com")

    resp = await http_client.post(
        _adjust_url(target_id),
        headers=_auth(admin_token),
        json={
            "amount_micro_rub": 500_000_000,
            "reason": "Возмещение за сбой",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["balance_micro_rub"] == 500_000_000
    assert data["balance_display"] == "500.0000"

    txn = (
        await db_session.execute(
            select(Transaction).where(
                Transaction.id == _uuid.UUID(data["transaction_id"])
            )
        )
    ).scalar_one()
    assert txn.type == "adjustment"
    assert txn.amount_micro_rub == 500_000_000
    assert txn.admin_reason == "Возмещение за сбой"

    audit = (
        await db_session.execute(
            select(AdminAuditLog).where(
                AdminAuditLog.action == "admin.user.adjust_balance",
                AdminAuditLog.target_id == target_id,
            )
        )
    ).scalar_one()
    assert audit.details["amount_micro_rub"] == 500_000_000
    assert audit.details["new_balance_micro_rub"] == 500_000_000


async def test_adjust_balance_requires_reason(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "u83_admin@example.com"
    )
    _, target_id = await _register(http_client, "u83_target_reason@example.com")

    # Empty reason → Pydantic 422 (min_length=5).
    resp = await http_client.post(
        _adjust_url(target_id),
        headers=_auth(admin_token),
        json={"amount_micro_rub": 100_000_000, "reason": ""},
    )
    assert resp.status_code == 422, resp.text

    # No transaction created.
    txns = (
        await db_session.execute(
            select(Transaction).where(
                Transaction.user_id == _uuid.UUID(target_id)
            )
        )
    ).scalars().all()
    assert txns == []


async def test_adjust_balance_overflow_amount_returns_422_not_500(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    """tr-lzmr: amount_micro_rub had no bound, so a value outside Postgres
    BIGINT range reached the transactions INSERT as a raw asyncpg.DataError
    (value out of int64 range) and surfaced as an opaque 500 instead of a
    clean 422 at the schema layer.
    """
    admin_token, _ = await _register_admin(
        http_client, db_session, "u83_admin@example.com"
    )
    _, target_id = await _register(http_client, "u83_target_reason@example.com")

    resp = await http_client.post(
        _adjust_url(target_id),
        headers=_auth(admin_token),
        json={"amount_micro_rub": 99_999_999_999_999_999_999_999_999, "reason": "overflow test"},
    )
    assert resp.status_code == 422, resp.text

    txns = (
        await db_session.execute(
            select(Transaction).where(
                Transaction.user_id == _uuid.UUID(target_id)
            )
        )
    ).scalars().all()
    assert txns == []


async def test_adjust_balance_both_amount_fields_returns_422_not_500(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    """tr-azhb: AdjustBalanceRequest.exactly_one_amount raises a plain
    ValueError when both `amount` and `amount_micro_rub` are set. Pydantic
    embeds that ValueError instance in the resulting error's `ctx.error`,
    which used to blow up main.py's validation_error_handler (json.dumps on a
    raw exception object) and mask the intended 422 as a 500 internal_error.
    """
    admin_token, _ = await _register_admin(
        http_client, db_session, "u83_admin@example.com"
    )
    _, target_id = await _register(http_client, "u83_target_reason@example.com")

    resp = await http_client.post(
        _adjust_url(target_id),
        headers=_auth(admin_token),
        json={"amount": 1, "amount_micro_rub": 1_000_000, "reason": "both provided"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "validation_error"

    txns = (
        await db_session.execute(
            select(Transaction).where(
                Transaction.user_id == _uuid.UUID(target_id)
            )
        )
    ).scalars().all()
    assert txns == []
