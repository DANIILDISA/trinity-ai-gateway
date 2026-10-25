"""Daily reconciliation tests (Task 9.4 — BILLING_LOGIC.md §11.1)."""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, time, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete as sa_delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.alert import Alert
from app.models.notification import Notification
from app.models.reservation import Reservation
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.tasks import reconciliation


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(autouse=True)
async def _stub_telegram(monkeypatch: pytest.MonkeyPatch):
    """Capture Telegram summary calls. Critical alert email is also captured
    so the per-check critical alerts don't try to hit Resend."""
    telegram_calls: list[dict] = []
    email_calls: list[dict] = []

    async def _tg(message: str, severity: str) -> None:
        telegram_calls.append({"message": message, "severity": severity})

    async def _email(title: str, message: str) -> None:
        email_calls.append({"title": title, "message": message})

    # Stub the names imported into reconciliation.py + alert_service.
    monkeypatch.setattr(reconciliation, "send_telegram_alert", _tg)
    from app.services import alert_service

    monkeypatch.setattr(alert_service, "send_telegram_alert", _tg)
    monkeypatch.setattr(alert_service, "send_alert_email", _email)
    return {"telegram": telegram_calls, "email": email_calls}


@pytest_asyncio.fixture(autouse=True)
async def _clean_slate(db_session: AsyncSession):
    """Reset transient state so the global SQL checks are deterministic.
    Users themselves are left in place (other test files may have created
    them) but their balances/reserved are zeroed so CHECK 1 sees no mismatch
    unless this test deliberately seeds one."""
    # Wipe FK refs to users first, then zero out balances. Notifications can
    # be left for other-user data, but we wipe to keep CHECK 2/4 cross-test
    # clean.
    await db_session.execute(sa_delete(UsageLog))
    await db_session.execute(sa_delete(Transaction))
    await db_session.execute(sa_delete(Reservation))
    await db_session.execute(sa_delete(Notification))
    await db_session.execute(
        sa_delete(Alert).where(Alert.source == "billing")
    )
    # CHECK 1 invariant: balance_micro_rub = SUM(transactions). With all
    # transactions wiped, every user must have balance=0 to pass.
    await db_session.execute(
        text("UPDATE users SET reserved_micro_rub = 0, balance_micro_rub = 0")
    )
    await db_session.commit()
    yield


async def _create_user(db: AsyncSession, prefix: str) -> User:
    # Unique email per call so tests don't collide on a previous run's row.
    email = f"{prefix}_{_uuid.uuid4().hex[:10]}@example.com"
    user = User(
        email=email,
        password_hash=await hash_password("Password1"),
        display_name="Сверка",
        role="user",
        status="active",
        balance_micro_rub=0,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _yesterday_at(hour: int = 12) -> datetime:
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    return datetime.combine(yesterday, time(hour=hour), tzinfo=timezone.utc)


async def _billing_alerts(db: AsyncSession) -> list[Alert]:
    return (
        await db.execute(
            select(Alert).where(Alert.source == "billing").order_by(Alert.created_at)
        )
    ).scalars().all()


# ── Scenario 1: clean slate → all checks pass ───────────────────────────────


async def test_clean_data_passes_all_checks_and_sends_info_summary(
    db_session: AsyncSession, _stub_telegram: dict
) -> None:
    result = await reconciliation.run_daily_reconciliation(db_session)

    assert result.balance_check_passed is True
    assert result.balance_mismatches == 0
    assert result.stale_reservations == 0
    assert result.orphaned_logs == 0
    # No yesterday traffic → margin stays None.
    assert result.margin_percent is None
    assert result.all_passed is True

    # No billing alerts created.
    assert await _billing_alerts(db_session) == []

    # Single info summary on Telegram.
    assert len(_stub_telegram["telegram"]) == 1
    summary = _stub_telegram["telegram"][0]
    assert summary["severity"] == "info"
    assert "Daily reconciliation OK" in summary["message"]


# ── CHECK 1: corrupted user balance ─────────────────────────────────────────


async def test_balance_mismatch_creates_critical_alert(
    db_session: AsyncSession, _stub_telegram: dict
) -> None:
    user = await _create_user(db_session, "rec_balance")
    expected_email = user.email
    # Plant a mismatch: balance says 5 ₽, but no transactions exist.
    await db_session.execute(
        text("UPDATE users SET balance_micro_rub = 5000000 WHERE id = :uid"),
        {"uid": user.id},
    )
    await db_session.commit()

    result = await reconciliation.run_daily_reconciliation(db_session)

    assert result.balance_check_passed is False
    assert result.balance_mismatches == 1

    alerts = await _billing_alerts(db_session)
    critical = [a for a in alerts if a.severity == "critical"]
    assert len(critical) == 1
    assert "Balance mismatch" in critical[0].title
    assert expected_email in critical[0].title
    assert critical[0].details["user_id"] == str(user.id)
    assert critical[0].details["stored_balance_micro_rub"] == 5_000_000
    assert critical[0].details["computed_balance_micro_rub"] == 0

    # Final Telegram call is the summary (critical because critical alerts fired).
    assert len(_stub_telegram["telegram"]) >= 1
    summary = _stub_telegram["telegram"][-1]
    assert summary["severity"] == "critical"
    assert "Reconciliation issues detected" in summary["message"]
    # Email fired for the critical alert (via create_alert).
    assert len(_stub_telegram["email"]) >= 1
    assert any("Balance mismatch" in c["title"] for c in _stub_telegram["email"])


# ── CHECK 2: stale active reservation ──────────────────────────────────────


async def test_stale_reservation_creates_warning_alert(
    db_session: AsyncSession, _stub_telegram: dict
) -> None:
    user = await _create_user(db_session, "rec_stale")
    # Bump reserved so the CHECK constraint balance >= reserved holds; balance
    # was just zeroed by _clean_slate, raise it to cover the reservation.
    await db_session.execute(
        text("UPDATE users SET balance_micro_rub = 100000000, reserved_micro_rub = 100000000 WHERE id = :uid"),
        {"uid": user.id},
    )
    res = Reservation(
        user_id=user.id,
        request_id=_uuid.uuid4(),
        status="active",
        reserved_micro_rub=100_000_000,
        model_id="openai/gpt-4o",
        estimated_input_tokens=10,
        estimated_output_tokens=10,
    )
    db_session.add(res)
    await db_session.commit()
    # Plant matching debit transactions so CHECK 1 passes (balance=100 ₽ with
    # no transactions would otherwise mismatch).
    db_session.add(
        Transaction(
            user_id=user.id,
            type="credit",
            amount_micro_rub=100_000_000,
            balance_after_micro_rub=100_000_000,
            description="seed",
            idempotency_key=f"seed-{_uuid.uuid4()}",
        )
    )
    # Backdate the reservation to 20 minutes ago.
    await db_session.execute(
        text("UPDATE reservations SET created_at = now() - interval '20 minutes' WHERE id = :rid"),
        {"rid": res.id},
    )
    await db_session.commit()

    result = await reconciliation.run_daily_reconciliation(db_session)

    assert result.stale_reservations == 1
    alerts = await _billing_alerts(db_session)
    warnings = [a for a in alerts if a.severity == "warning"]
    assert len(warnings) == 1
    assert "stale reservations" in warnings[0].title

    # Final Telegram call is the summary (warning — no criticals).
    summary = _stub_telegram["telegram"][-1]
    assert summary["severity"] == "warning"
    assert "Reconciliation issues detected" in summary["message"]


# ── CHECK 3: negative margin with overdraft adjustment ──────────────────────


async def test_negative_margin_creates_critical_alert(
    db_session: AsyncSession, _stub_telegram: dict
) -> None:
    user = await _create_user(db_session, "rec_margin")
    when = _yesterday_at(12)
    request_id = _uuid.uuid4()

    # Books must balance for CHECK 1: credit + debit nets to 0 → user balance=0.
    db_session.add(
        Transaction(
            user_id=user.id,
            type="credit",
            amount_micro_rub=1_000_000,
            balance_after_micro_rub=1_000_000,
            description="rec test seed credit",
            idempotency_key=f"rec-seed-{request_id}",
            created_at=when - timedelta(minutes=1),
        )
    )
    # Revenue = 1 ₽ (debit), Cost = 0.10 USD × 82.5 ₽/USD ≈ 8.25 ₽
    # → margin = (1 - 8.25) / 8.25 × 100 ≈ -87.9% (critical).
    db_session.add(
        Transaction(
            user_id=user.id,
            type="debit",
            amount_micro_rub=-1_000_000,
            balance_after_micro_rub=0,
            description="rec test debit",
            request_id=request_id,
            idempotency_key=f"rec-debit-{request_id}",
            created_at=when,
        )
    )
    db_session.add(
        UsageLog(
            user_id=user.id,
            request_id=request_id,
            model_id="openai/gpt-4o",
            input_tokens=100,
            output_tokens=100,
            estimated_input_tokens=100,
            estimated_output_tokens=100,
            estimated_cost_micro_rub=1_000_000,
            actual_cost_micro_rub=1_000_000,
            reserved_micro_rub=2_000_000,
            overdraft_absorbed_micro_rub=0,
            openrouter_cost_micro_usd=100_000,  # 0.10 USD
            exchange_rate_micro_rub=82_500_000,
            status="completed",
            created_at=when,
        )
    )
    await db_session.commit()

    result = await reconciliation.run_daily_reconciliation(db_session)

    assert result.margin_percent is not None
    assert result.margin_percent < 0
    alerts = await _billing_alerts(db_session)
    critical = [a for a in alerts if a.severity == "critical" and "Negative margin" in a.title]
    assert len(critical) == 1
    assert _stub_telegram["telegram"][-1]["severity"] == "critical"


async def test_overdraft_absorbed_factored_into_pricing_margin(
    db_session: AsyncSession, _stub_telegram: dict
) -> None:
    """Margin<10% with pricing_margin≥20% should fire INFO (not warning)."""
    user = await _create_user(db_session, "rec_overdraft")
    when = _yesterday_at(12)
    request_id = _uuid.uuid4()

    # Seed credit so the books balance for CHECK 1.
    db_session.add(
        Transaction(
            user_id=user.id,
            type="credit",
            amount_micro_rub=8_500_000,
            balance_after_micro_rub=8_500_000,
            description="rec test seed credit",
            idempotency_key=f"rec-od-seed-{request_id}",
            created_at=when - timedelta(minutes=1),
        )
    )
    # Cost = 0.10 USD × 82.5 = 8.25 ₽
    # Revenue (debit) = 8.50 ₽ → margin = (8.50 - 8.25) / 8.25 = 3.0% (<10)
    # Absorbed = 2 ₽ → effective = 10.50 → pricing_margin = (10.50 - 8.25)/8.25 = 27.3% (≥20)
    # → info alert.
    db_session.add(
        Transaction(
            user_id=user.id,
            type="debit",
            amount_micro_rub=-8_500_000,
            balance_after_micro_rub=0,
            description="rec test debit",
            request_id=request_id,
            idempotency_key=f"rec-od-{request_id}",
            created_at=when,
        )
    )
    db_session.add(
        UsageLog(
            user_id=user.id,
            request_id=request_id,
            model_id="openai/gpt-4o",
            input_tokens=100,
            output_tokens=100,
            estimated_input_tokens=100,
            estimated_output_tokens=100,
            estimated_cost_micro_rub=8_500_000,
            actual_cost_micro_rub=8_500_000,
            reserved_micro_rub=17_000_000,
            overdraft_absorbed_micro_rub=2_000_000,
            openrouter_cost_micro_usd=100_000,
            exchange_rate_micro_rub=82_500_000,
            status="completed",
            created_at=when,
        )
    )
    await db_session.commit()

    result = await reconciliation.run_daily_reconciliation(db_session)

    assert result.margin_percent is not None
    assert 0 <= result.margin_percent < 10
    assert result.pricing_margin_percent is not None
    assert result.pricing_margin_percent >= 20

    alerts = await _billing_alerts(db_session)
    info_margin = [a for a in alerts if a.severity == "info" and "Margin on" in a.title]
    assert len(info_margin) == 1
    # No critical / warning from this scenario (books balance, no stale, no orphans).
    assert not [a for a in alerts if a.severity in ("critical", "warning")]
    # Summary is info because nothing critical/warning fired.
    assert _stub_telegram["telegram"][-1]["severity"] == "info"


# ── CHECK 4: orphaned usage log ─────────────────────────────────────────────


async def test_orphaned_usage_log_creates_critical_alert(
    db_session: AsyncSession, _stub_telegram: dict
) -> None:
    user = await _create_user(db_session, "rec_orphan")
    when = _yesterday_at(12)
    request_id = _uuid.uuid4()

    # Completed usage_log with cost > 0 and NO matching debit transaction.
    db_session.add(
        UsageLog(
            user_id=user.id,
            request_id=request_id,
            model_id="openai/gpt-4o",
            input_tokens=100,
            output_tokens=100,
            estimated_input_tokens=100,
            estimated_output_tokens=100,
            estimated_cost_micro_rub=1_000_000,
            actual_cost_micro_rub=1_000_000,
            reserved_micro_rub=2_000_000,
            overdraft_absorbed_micro_rub=0,
            openrouter_cost_micro_usd=10_000,
            exchange_rate_micro_rub=82_500_000,
            status="completed",
            created_at=when,
        )
    )
    await db_session.commit()

    result = await reconciliation.run_daily_reconciliation(db_session)

    assert result.orphaned_logs == 1
    alerts = await _billing_alerts(db_session)
    orphan = [
        a for a in alerts
        if a.severity == "critical" and "no matching debit transaction" in a.title
    ]
    assert len(orphan) == 1
    assert _stub_telegram["telegram"][-1]["severity"] == "critical"


# ── Telegram summary always fires exactly once ──────────────────────────────


async def test_summary_telegram_fires_exactly_once_in_all_scenarios(
    db_session: AsyncSession, _stub_telegram: dict
) -> None:
    """Even when everything is clean, send_telegram_alert is called once."""
    await reconciliation.run_daily_reconciliation(db_session)
    assert len(_stub_telegram["telegram"]) == 1


