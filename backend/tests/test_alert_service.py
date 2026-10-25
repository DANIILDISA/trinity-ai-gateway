"""Tests for create_alert (BILLING_LOGIC.md §14.1)."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert
from app.services import alert_service


_TEST_SOURCE = "test_alert_service"


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(db_session: AsyncSession):
    await db_session.execute(sa_delete(Alert).where(Alert.source == _TEST_SOURCE))
    await db_session.commit()
    yield
    await db_session.execute(sa_delete(Alert).where(Alert.source == _TEST_SOURCE))
    await db_session.commit()


# Suppress real Telegram + email dispatch — these tests assert on the call,
# not on the resulting HTTP request. The mock replaces both with recording stubs.
@pytest_asyncio.fixture(autouse=True)
async def _stub_telegram(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict] = []

    async def _stub(message: str, severity: str) -> None:
        calls.append({"message": message, "severity": severity})

    monkeypatch.setattr(alert_service, "send_telegram_alert", _stub)
    return calls


@pytest_asyncio.fixture(autouse=True)
async def _stub_email(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict] = []

    async def _stub(title: str, message: str) -> None:
        calls.append({"title": title, "message": message})

    monkeypatch.setattr(alert_service, "send_alert_email", _stub)
    return calls


# ── Insert behaviour ────────────────────────────────────────────────────────


async def test_create_alert_critical_inserts_row(
    db_session: AsyncSession, _stub_telegram: list[dict]
) -> None:
    alert = await alert_service.create_alert(
        db_session,
        severity="critical",
        source=_TEST_SOURCE,
        title="Balance below threshold",
        message="OpenRouter balance is $5 (threshold $100)",
        details={"balance_usd": 5.0, "threshold_usd": 100.0},
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(Alert).where(Alert.id == alert.id)
        )
    ).scalar_one()
    assert row.severity == "critical"
    assert row.status == "new"
    assert row.source == _TEST_SOURCE
    assert row.title == "Balance below threshold"
    assert row.message == "OpenRouter balance is $5 (threshold $100)"
    assert row.details == {"balance_usd": 5.0, "threshold_usd": 100.0}


async def test_create_alert_warning_inserts_row(
    db_session: AsyncSession, _stub_telegram: list[dict]
) -> None:
    alert = await alert_service.create_alert(
        db_session,
        severity="warning",
        source=_TEST_SOURCE,
        title="OpenRouter error rate high",
    )
    await db_session.commit()
    row = (
        await db_session.execute(select(Alert).where(Alert.id == alert.id))
    ).scalar_one()
    assert row.severity == "warning"
    assert row.source == _TEST_SOURCE
    # message defaults to title when omitted.
    assert row.message == "OpenRouter error rate high"


async def test_create_alert_info_inserts_row(
    db_session: AsyncSession, _stub_telegram: list[dict]
) -> None:
    alert = await alert_service.create_alert(
        db_session,
        severity="info",
        source=_TEST_SOURCE,
        title="Metric heartbeat",
    )
    await db_session.commit()
    row = (
        await db_session.execute(select(Alert).where(Alert.id == alert.id))
    ).scalar_one()
    assert row.severity == "info"


# ── Telegram dispatch by severity ───────────────────────────────────────────


async def test_critical_dispatches_telegram(
    db_session: AsyncSession, _stub_telegram: list[dict]
) -> None:
    await alert_service.create_alert(
        db_session, severity="critical", source=_TEST_SOURCE, title="c"
    )
    await db_session.commit()
    assert _stub_telegram == [{"message": "c", "severity": "critical"}]


async def test_warning_dispatches_telegram(
    db_session: AsyncSession, _stub_telegram: list[dict]
) -> None:
    await alert_service.create_alert(
        db_session, severity="warning", source=_TEST_SOURCE, title="w"
    )
    await db_session.commit()
    assert _stub_telegram == [{"message": "w", "severity": "warning"}]


async def test_info_does_not_dispatch_telegram(
    db_session: AsyncSession, _stub_telegram: list[dict]
) -> None:
    await alert_service.create_alert(
        db_session, severity="info", source=_TEST_SOURCE, title="i"
    )
    await db_session.commit()
    assert _stub_telegram == []


# ── Email dispatch for critical alerts (Task 9.4 Part A) ────────────────────


async def test_critical_dispatches_email(
    db_session: AsyncSession,
    _stub_telegram: list[dict],
    _stub_email: list[dict],
) -> None:
    await alert_service.create_alert(
        db_session,
        severity="critical",
        source=_TEST_SOURCE,
        title="DB down",
        message="Primary DB unreachable for 60s",
    )
    await db_session.commit()
    assert _stub_email == [
        {"title": "DB down", "message": "Primary DB unreachable for 60s"}
    ]


async def test_critical_email_falls_back_to_title_when_message_omitted(
    db_session: AsyncSession,
    _stub_telegram: list[dict],
    _stub_email: list[dict],
) -> None:
    await alert_service.create_alert(
        db_session, severity="critical", source=_TEST_SOURCE, title="only-title"
    )
    await db_session.commit()
    assert _stub_email == [{"title": "only-title", "message": "only-title"}]


async def test_warning_does_not_dispatch_email(
    db_session: AsyncSession,
    _stub_telegram: list[dict],
    _stub_email: list[dict],
) -> None:
    await alert_service.create_alert(
        db_session, severity="warning", source=_TEST_SOURCE, title="w-no-email"
    )
    await db_session.commit()
    assert _stub_email == []


async def test_info_does_not_dispatch_email(
    db_session: AsyncSession,
    _stub_telegram: list[dict],
    _stub_email: list[dict],
) -> None:
    await alert_service.create_alert(
        db_session, severity="info", source=_TEST_SOURCE, title="i-no-email"
    )
    await db_session.commit()
    assert _stub_email == []
