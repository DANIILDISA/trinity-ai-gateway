"""Tests for the admin financial dashboard (Task 8.2)."""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AdminAuditLog
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.models.model_pricing import ModelPricing

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
DASHBOARD_URL = "/api/v1/admin/dashboard"
REVENUE_BY_MODEL_URL = "/api/v1/admin/dashboard/revenue-by-model"

_TEST_EMAILS = [
    "dash_admin@example.com",
    "dash_user_a@example.com",
    "dash_user_b@example.com",
]

# Use the first two seeded model_ids (002_seed_model_pricing.py).
_MODEL_A = "anthropic/claude-sonnet-4-6"
_MODEL_B = "openai/gpt-4.1"


@pytest_asyncio.fixture(autouse=True)
async def clean_state(db_session: AsyncSession) -> None:
    """Purge transactions, usage_logs, and test users before each test.

    These tests own the dashboard signal — leaking debits or usage_logs from
    earlier tests would make assertions flap.
    """
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
                sa_delete(AdminAuditLog).where(
                    AdminAuditLog.target_id == str(existing.id)
                )
            )
            await db_session.delete(existing)
    # Also clear any orphan rows from prior test runs that might still match
    # admin endpoints' time windows.
    await db_session.execute(sa_delete(UsageLog))
    await db_session.execute(sa_delete(Transaction))
    await db_session.commit()


async def _register(http_client: AsyncClient, email: str) -> tuple[str, str]:
    resp = await http_client.post(
        REGISTER_URL,
        json={"email": email, "password": "SecurePass1", "display_name": "Имя"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return data["access_token"], data["user"]["id"]


async def _register_admin(
    http_client: AsyncClient, db_session: AsyncSession, email: str
) -> tuple[str, str]:
    token, user_id = await _register(http_client, email)
    user = (
        await db_session.execute(
            select(User).where(User.id == _uuid.UUID(user_id))
        )
    ).scalar_one()
    user.role = "admin"
    await db_session.commit()
    login = await http_client.post(
        LOGIN_URL, json={"email": email, "password": "SecurePass1"}
    )
    assert login.status_code == 200
    return login.json()["access_token"], user_id


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_usage_log(
    db: AsyncSession,
    *,
    user_id: _uuid.UUID,
    model_id: str,
    when: datetime,
    actual_cost_micro_rub: int,
    openrouter_cost_micro_usd: int,
    overdraft_absorbed_micro_rub: int = 0,
    input_tokens: int = 100,
    output_tokens: int = 200,
    status: str = "completed",
    cost_pending: bool = False,
) -> UsageLog:
    log = UsageLog(
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
        overdraft_absorbed_micro_rub=overdraft_absorbed_micro_rub,
        openrouter_cost_micro_usd=openrouter_cost_micro_usd,
        cost_pending=cost_pending,
        exchange_rate_micro_rub=82_500_000,
        status=status,
        created_at=when,
    )
    db.add(log)
    return log


async def _seed_debit_transaction(
    db: AsyncSession,
    *,
    user_id: _uuid.UUID,
    when: datetime,
    amount_micro_rub: int,
    description: str = "test debit",
) -> Transaction:
    # debit amounts MUST be negative (chk_debit_negative).
    txn = Transaction(
        user_id=user_id,
        type="debit",
        amount_micro_rub=-abs(amount_micro_rub),
        balance_after_micro_rub=0,
        description=description,
        request_id=_uuid.uuid4(),
        idempotency_key=f"test:{_uuid.uuid4()}",
        created_at=when,
    )
    db.add(txn)
    return txn


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_dashboard_empty(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "dash_admin@example.com"
    )

    resp = await http_client.get(DASHBOARD_URL, headers=_auth(admin_token))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["revenue"]["today_micro_rub"] == 0
    assert data["revenue"]["week_micro_rub"] == 0
    assert data["revenue"]["month_micro_rub"] == 0
    assert data["costs"]["today_usd"] == 0
    assert data["margin"]["today_percent"] == 0.0
    assert data["active_users_today"] == 0
    assert data["total_requests_today"] == 0
    # Reconciliation field shape — other test files may leave users with
    # non-zero balances that don't reconcile against the wiped ledger.
    assert data["reconciliation"]["status"] in ("ok", "discrepancy")
    assert 0.0 <= data["reconciliation"]["discrepancy_percent"] <= 100.0


async def test_dashboard_with_data(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "dash_admin@example.com"
    )
    _, user_a = await _register(http_client, "dash_user_a@example.com")
    _, user_b = await _register(http_client, "dash_user_b@example.com")

    uid_a, uid_b = _uuid.UUID(user_a), _uuid.UUID(user_b)
    now = datetime.now(timezone.utc)
    today = now - timedelta(hours=1)

    # Two completed requests + matching debits today.
    await _seed_usage_log(
        db_session,
        user_id=uid_a,
        model_id=_MODEL_A,
        when=today,
        actual_cost_micro_rub=1_000_000,  # 1 RUB
        openrouter_cost_micro_usd=10_000,  # 0.01 USD
    )
    await _seed_usage_log(
        db_session,
        user_id=uid_b,
        model_id=_MODEL_B,
        when=today,
        actual_cost_micro_rub=2_000_000,  # 2 RUB
        openrouter_cost_micro_usd=20_000,  # 0.02 USD
    )
    await _seed_debit_transaction(
        db_session, user_id=uid_a, when=today, amount_micro_rub=1_000_000
    )
    await _seed_debit_transaction(
        db_session, user_id=uid_b, when=today, amount_micro_rub=2_000_000
    )
    await db_session.commit()

    resp = await http_client.get(DASHBOARD_URL, headers=_auth(admin_token))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["revenue"]["today_micro_rub"] == 3_000_000
    assert data["revenue"]["today_display"] == "3.0000"
    # 0.03 USD total cost.
    assert abs(data["costs"]["today_usd"] - 0.03) < 1e-6
    # effective_revenue=3_000_000 µ₽, cost=30_000 × 82_500_000 / 10^6 = 2_475_000 µ₽
    # margin = (3_000_000 - 2_475_000) / 3_000_000 × 100 ≈ 17.5%
    assert 17.0 < data["margin"]["today_percent"] < 18.0

    # Both seeded users are active today + 2 total requests.
    assert data["active_users_today"] == 2
    assert data["total_requests_today"] == 2


async def test_dashboard_overdraft_reduces_margin(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Margin must use effective_revenue = gross_revenue − overdraft_absorbed."""
    admin_token, _ = await _register_admin(
        http_client, db_session, "dash_admin@example.com"
    )
    _, user_a = await _register(http_client, "dash_user_a@example.com")
    uid_a = _uuid.UUID(user_a)

    now = datetime.now(timezone.utc) - timedelta(hours=1)

    # Real cost was 3 RUB but only 2 RUB was charged (1 RUB absorbed).
    # Gross revenue (debits) = 2 RUB. Overdraft absorbed = 1 RUB.
    # Effective revenue = 1 RUB. Cost = 0.02 USD * 82.5 RUB/USD = 1.65 RUB → margin negative.
    await _seed_usage_log(
        db_session,
        user_id=uid_a,
        model_id=_MODEL_A,
        when=now,
        actual_cost_micro_rub=2_000_000,
        openrouter_cost_micro_usd=20_000,
        overdraft_absorbed_micro_rub=1_000_000,
    )
    await _seed_debit_transaction(
        db_session, user_id=uid_a, when=now, amount_micro_rub=2_000_000
    )
    await db_session.commit()

    resp = await http_client.get(DASHBOARD_URL, headers=_auth(admin_token))
    assert resp.status_code == 200, resp.text
    margin_with_overdraft = resp.json()["margin"]["today_percent"]

    # Now wipe the overdraft and verify margin improves.
    await db_session.execute(
        sa_delete(UsageLog).where(UsageLog.user_id == uid_a)
    )
    await _seed_usage_log(
        db_session,
        user_id=uid_a,
        model_id=_MODEL_A,
        when=now,
        actual_cost_micro_rub=2_000_000,
        openrouter_cost_micro_usd=20_000,
        overdraft_absorbed_micro_rub=0,
    )
    await db_session.commit()

    resp2 = await http_client.get(DASHBOARD_URL, headers=_auth(admin_token))
    assert resp2.status_code == 200, resp2.text
    margin_without_overdraft = resp2.json()["margin"]["today_percent"]

    assert margin_without_overdraft > margin_with_overdraft, (
        f"absorbing overdraft should lower margin: "
        f"with={margin_with_overdraft}, without={margin_without_overdraft}"
    )


async def test_dashboard_exposes_overdraft_and_effective_revenue(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    """tr-yalc: the dashboard must SURFACE the absorbed overdraft and the
    effective revenue the margin is based on, not just a bare margin %."""
    admin_token, _ = await _register_admin(
        http_client, db_session, "dash_admin@example.com"
    )
    _, user_a = await _register(http_client, "dash_user_a@example.com")
    uid_a = _uuid.UUID(user_a)
    now = datetime.now(timezone.utc) - timedelta(hours=1)

    # Gross revenue (debits) = 5 RUB; 2 RUB of cost absorbed as overdraft.
    await _seed_usage_log(
        db_session,
        user_id=uid_a,
        model_id=_MODEL_A,
        when=now,
        actual_cost_micro_rub=5_000_000,
        openrouter_cost_micro_usd=20_000,
        overdraft_absorbed_micro_rub=2_000_000,
    )
    await _seed_debit_transaction(
        db_session, user_id=uid_a, when=now, amount_micro_rub=5_000_000
    )
    await db_session.commit()

    data = (await http_client.get(DASHBOARD_URL, headers=_auth(admin_token))).json()
    assert data["overdraft"]["today_micro_rub"] == 2_000_000
    assert data["overdraft"]["today_display"]  # human string present
    # effective revenue = gross revenue − absorbed overdraft = 5 − 2 = 3 RUB.
    assert data["effective_revenue"]["today_micro_rub"] == 3_000_000


async def test_revenue_by_model_ordering(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "dash_admin@example.com"
    )
    _, user_a = await _register(http_client, "dash_user_a@example.com")
    uid_a = _uuid.UUID(user_a)
    now = datetime.now(timezone.utc) - timedelta(hours=1)

    # Model A: 1 request, 1 RUB. Model B: 2 requests, total 5 RUB.
    await _seed_usage_log(
        db_session,
        user_id=uid_a,
        model_id=_MODEL_A,
        when=now,
        actual_cost_micro_rub=1_000_000,
        openrouter_cost_micro_usd=10_000,
    )
    await _seed_usage_log(
        db_session,
        user_id=uid_a,
        model_id=_MODEL_B,
        when=now,
        actual_cost_micro_rub=2_000_000,
        openrouter_cost_micro_usd=20_000,
    )
    await _seed_usage_log(
        db_session,
        user_id=uid_a,
        model_id=_MODEL_B,
        when=now,
        actual_cost_micro_rub=3_000_000,
        openrouter_cost_micro_usd=25_000,
    )
    await db_session.commit()

    resp = await http_client.get(
        REVENUE_BY_MODEL_URL, headers=_auth(admin_token)
    )
    assert resp.status_code == 200, resp.text
    models = resp.json()["models"]
    assert len(models) == 2
    # Model B leads (5 RUB > 1 RUB).
    assert models[0]["model_id"] == _MODEL_B
    assert models[0]["revenue_micro_rub"] == 5_000_000
    assert models[0]["request_count"] == 2
    assert models[1]["model_id"] == _MODEL_A
    assert models[1]["revenue_micro_rub"] == 1_000_000
    assert models[1]["request_count"] == 1


async def test_revenue_by_model_pending_cost_does_not_fake_full_margin(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    """tr-34mj: a charged request whose OpenRouter cost is still UNKNOWN
    (cost_pending) must NOT read as ~100% margin. The per-model margin is computed
    only over known-cost rows; with no known cost it is a conservative 0%, while
    the displayed revenue still reflects the real charge."""
    admin_token, _ = await _register_admin(
        http_client, db_session, "dash_admin@example.com"
    )
    _, user_a = await _register(http_client, "dash_user_a@example.com")
    uid_a = _uuid.UUID(user_a)
    now = datetime.now(timezone.utc) - timedelta(hours=1)

    # One completed request: user charged 10 ₽, but OpenRouter cost not captured.
    await _seed_usage_log(
        db_session,
        user_id=uid_a,
        model_id=_MODEL_A,
        when=now,
        actual_cost_micro_rub=10_000_000,
        openrouter_cost_micro_usd=0,
        cost_pending=True,
    )
    await db_session.commit()

    resp = await http_client.get(REVENUE_BY_MODEL_URL, headers=_auth(admin_token))
    assert resp.status_code == 200, resp.text
    row = next(m for m in resp.json()["models"] if m["model_id"] == _MODEL_A)
    assert row["revenue_micro_rub"] == 10_000_000  # real charge still shown
    assert row["margin_percent"] == 0.0  # NOT a fake 100%


async def test_dashboard_period_filter(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    """period='today' must exclude rows older than today's UTC midnight."""
    admin_token, _ = await _register_admin(
        http_client, db_session, "dash_admin@example.com"
    )
    _, user_a = await _register(http_client, "dash_user_a@example.com")
    uid_a = _uuid.UUID(user_a)

    now = datetime.now(timezone.utc)
    today = now - timedelta(hours=1)
    yesterday = now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(hours=2)

    # Today: 1 request + 1 debit.
    await _seed_usage_log(
        db_session,
        user_id=uid_a,
        model_id=_MODEL_A,
        when=today,
        actual_cost_micro_rub=1_000_000,
        openrouter_cost_micro_usd=10_000,
    )
    await _seed_debit_transaction(
        db_session, user_id=uid_a, when=today, amount_micro_rub=1_000_000
    )

    # Yesterday: 1 request + 1 debit (5 RUB).
    await _seed_usage_log(
        db_session,
        user_id=uid_a,
        model_id=_MODEL_A,
        when=yesterday,
        actual_cost_micro_rub=5_000_000,
        openrouter_cost_micro_usd=50_000,
    )
    await _seed_debit_transaction(
        db_session, user_id=uid_a, when=yesterday, amount_micro_rub=5_000_000
    )
    await db_session.commit()

    resp = await http_client.get(DASHBOARD_URL, headers=_auth(admin_token))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Today excludes yesterday's data.
    assert data["revenue"]["today_micro_rub"] == 1_000_000
    assert data["total_requests_today"] == 1
    assert data["active_users_today"] == 1
    # Week includes both (7-day rolling window).
    assert data["revenue"]["week_micro_rub"] == 6_000_000

    # revenue-by-model with period=today returns only today's model.
    resp_models = await http_client.get(
        REVENUE_BY_MODEL_URL,
        headers=_auth(admin_token),
        params={"period": "today"},
    )
    assert resp_models.status_code == 200, resp_models.text
    models = resp_models.json()["models"]
    assert len(models) == 1
    assert models[0]["revenue_micro_rub"] == 1_000_000

    # With period=week, the older entry shows up too (same model, summed).
    resp_models_week = await http_client.get(
        REVENUE_BY_MODEL_URL,
        headers=_auth(admin_token),
        params={"period": "week"},
    )
    assert resp_models_week.status_code == 200, resp_models_week.text
    week_models = resp_models_week.json()["models"]
    assert len(week_models) == 1
    assert week_models[0]["revenue_micro_rub"] == 6_000_000


# Ensure seeded model fixtures exist (sanity check used by ModelPricing FK).
async def test_seeded_models_present(db_session: AsyncSession) -> None:
    rows = (
        await db_session.execute(
            select(ModelPricing.model_id).where(
                ModelPricing.is_current.is_(True),
                ModelPricing.model_id.in_([_MODEL_A, _MODEL_B]),
            )
        )
    ).scalars().all()
    assert set(rows) == {_MODEL_A, _MODEL_B}
