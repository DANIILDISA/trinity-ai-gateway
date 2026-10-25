"""Admin manual balance adjustment tests."""
import uuid as _uuid

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AdminAuditLog
from app.models.transaction import Transaction
from app.models.user import User
from app.services.billing_service import credit_balance

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"

_TEST_EMAILS = [
    "admin_actor@example.com",
    "admin_target_credit@example.com",
    "admin_target_debit@example.com",
    "admin_target_clamp@example.com",
    "admin_target_short@example.com",
    "admin_target_forbidden@example.com",
    "admin_nonadmin@example.com",
    "admin_target_audit@example.com",
]


@pytest_asyncio.fixture(autouse=True)
async def clean_test_users(db_session: AsyncSession) -> None:
    for email in _TEST_EMAILS:
        existing = (
            await db_session.execute(
                select(User).where(User.email_lower == email.lower())
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Adjustment transactions reference the admin via admin_user_id;
            # purge both ownership and admin-actor links to avoid FK violations.
            await db_session.execute(
                sa_delete(Transaction).where(Transaction.user_id == existing.id)
            )
            await db_session.execute(
                sa_delete(Transaction).where(Transaction.admin_user_id == existing.id)
            )
            await db_session.execute(
                sa_delete(AdminAuditLog).where(AdminAuditLog.actor_id == existing.id)
            )
            await db_session.execute(
                sa_delete(AdminAuditLog).where(
                    AdminAuditLog.target_id == str(existing.id)
                )
            )
            await db_session.delete(existing)
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
    """Register a user and promote to admin in the DB."""
    token, user_id = await _register(http_client, email)
    user = (
        await db_session.execute(
            select(User).where(User.id == _uuid.UUID(user_id))
        )
    ).scalar_one()
    user.role = "admin"
    await db_session.commit()
    # Re-login so the access token is fresh (role in token claims may matter
    # for future implementations; require_admin re-reads from DB so even the
    # original token works, but a fresh one is cleaner).
    login = await http_client.post(
        LOGIN_URL, json={"email": email, "password": "SecurePass1"}
    )
    assert login.status_code == 200
    return login.json()["access_token"], user_id


def _adjust_url(user_id: str) -> str:
    return f"/api/v1/admin/users/{user_id}/adjust-balance"


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_admin_credit(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "admin_actor@example.com"
    )
    _, target_id = await _register(http_client, "admin_target_credit@example.com")

    resp = await http_client.post(
        _adjust_url(target_id),
        json={"amount": 100, "reason": "Компенсация за сбой"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["balance_micro_rub"] == 100_000_000
    assert data["balance"] == "100.0000"

    txn = (
        await db_session.execute(
            select(Transaction).where(Transaction.id == _uuid.UUID(data["transaction_id"]))
        )
    ).scalar_one()
    assert txn.type == "adjustment"
    assert txn.amount_micro_rub == 100_000_000
    assert txn.balance_after_micro_rub == 100_000_000
    assert txn.admin_reason == "Компенсация за сбой"


async def test_admin_debit(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "admin_actor@example.com"
    )
    _, target_id = await _register(http_client, "admin_target_debit@example.com")

    # Seed target with 200₽
    await credit_balance(
        db_session,
        user_id=_uuid.UUID(target_id),
        amount_micro_rub=200_000_000,
        idempotency_key=f"seed-debit-{_uuid.uuid4()}",
        description="Test seed",
    )
    await db_session.commit()

    resp = await http_client.post(
        _adjust_url(target_id),
        json={"amount": -50, "reason": "Возврат за тест"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["balance_micro_rub"] == 150_000_000
    assert data["balance"] == "150.0000"

    txn = (
        await db_session.execute(
            select(Transaction).where(Transaction.id == _uuid.UUID(data["transaction_id"]))
        )
    ).scalar_one()
    assert txn.type == "adjustment"
    assert txn.amount_micro_rub == -50_000_000
    assert txn.balance_after_micro_rub == 150_000_000


async def test_admin_debit_exceeds_available_422(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Over-available debit is REJECTED with 422 (was a silent clamp to -100M)."""
    admin_token, _ = await _register_admin(
        http_client, db_session, "admin_actor@example.com"
    )
    _, target_id = await _register(http_client, "admin_target_clamp@example.com")
    await credit_balance(
        db_session,
        user_id=_uuid.UUID(target_id),
        amount_micro_rub=100_000_000,
        idempotency_key=f"seed-clamp-{_uuid.uuid4()}",
        description="Test seed",
    )
    await db_session.commit()

    resp = await http_client.post(
        _adjust_url(target_id),
        json={"amount": -300, "reason": "Большой возврат для теста клампа"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422, resp.text
    # Nothing written — no transaction row created (reliable, not identity-map cached).
    txns = (
        await db_session.execute(
            select(Transaction).where(
                Transaction.user_id == _uuid.UUID(target_id),
                Transaction.type == "adjustment",
            )
        )
    ).scalars().all()
    assert txns == []  # no debit applied (the seed credit txn is excluded)


async def test_admin_debit_into_reserved_rejected_422(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Repro of the 500: debiting past (balance - reserved) violated chk_available_balance."""
    admin_token, _ = await _register_admin(
        http_client, db_session, "admin_actor@example.com"
    )
    _, target_id = await _register(http_client, "admin_target_reserved@example.com")
    await credit_balance(
        db_session,
        user_id=_uuid.UUID(target_id),
        amount_micro_rub=100_000_000,
        idempotency_key=f"seed-res-{_uuid.uuid4()}",
        description="Test seed",
    )
    user = (
        await db_session.execute(select(User).where(User.id == _uuid.UUID(target_id)))
    ).scalar_one()
    user.reserved_micro_rub = 80_000_000  # available = 20₽
    await db_session.commit()

    resp = await http_client.post(
        _adjust_url(target_id),
        json={"amount_micro_rub": -100_000_000, "reason": "Списание при резерве"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    # Was an unhandled 500 (chk_available_balance violation); now a clean 422 with no write.
    assert resp.status_code == 422, resp.text
    txns = (
        await db_session.execute(
            select(Transaction).where(
                Transaction.user_id == _uuid.UUID(target_id),
                Transaction.type == "adjustment",
            )
        )
    ).scalars().all()
    assert txns == []  # no debit applied (the seed credit txn is excluded)


async def test_admin_debit_within_available_succeeds(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "admin_actor@example.com"
    )
    _, target_id = await _register(http_client, "admin_target_within@example.com")
    await credit_balance(
        db_session,
        user_id=_uuid.UUID(target_id),
        amount_micro_rub=100_000_000,
        idempotency_key=f"seed-within-{_uuid.uuid4()}",
        description="Test seed",
    )
    user = (
        await db_session.execute(select(User).where(User.id == _uuid.UUID(target_id)))
    ).scalar_one()
    user.reserved_micro_rub = 80_000_000  # available = 20₽
    await db_session.commit()

    # Debit exactly the available 20₽ → succeeds, full amount, constraint holds.
    resp = await http_client.post(
        _adjust_url(target_id),
        json={"amount_micro_rub": -20_000_000, "reason": "Списание в пределах доступного"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["balance_micro_rub"] == 80_000_000
    # Refresh past the identity-map cache to read the endpoint's committed values.
    await db_session.refresh(user)
    assert user.balance_micro_rub == 80_000_000
    assert user.reserved_micro_rub == 80_000_000


async def test_admin_short_reason(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "admin_actor@example.com"
    )
    _, target_id = await _register(http_client, "admin_target_short@example.com")

    resp = await http_client.post(
        _adjust_url(target_id),
        json={"amount": 100, "reason": "hi"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422

    # Verify no transaction was created
    txns = (
        await db_session.execute(
            select(Transaction).where(
                Transaction.user_id == _uuid.UUID(target_id),
                Transaction.type == "adjustment",
            )
        )
    ).scalars().all()
    assert txns == []  # no debit applied (the seed credit txn is excluded)


async def test_admin_non_admin_forbidden(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    # Regular user — not promoted to admin
    user_token, _ = await _register(http_client, "admin_nonadmin@example.com")
    _, target_id = await _register(http_client, "admin_target_forbidden@example.com")

    resp = await http_client.post(
        _adjust_url(target_id),
        json={"amount": 100, "reason": "Should be forbidden"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert resp.status_code == 403


async def test_admin_audit_log_created(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, admin_id = await _register_admin(
        http_client, db_session, "admin_actor@example.com"
    )
    _, target_id = await _register(http_client, "admin_target_audit@example.com")

    resp = await http_client.post(
        _adjust_url(target_id),
        json={"amount": 250, "reason": "Тестовая запись аудита"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200

    audit = (
        await db_session.execute(
            select(AdminAuditLog).where(
                AdminAuditLog.action == "admin.user.adjust_balance",
                AdminAuditLog.target_id == target_id,
            )
        )
    ).scalar_one()
    assert audit.actor_id == _uuid.UUID(admin_id)
    assert audit.actor_email == "admin_actor@example.com"
    assert audit.target_type == "user"
    assert audit.details is not None
    # New micro-rub audit field (Task 8.3). 250 RUB = 250_000_000 µ₽.
    assert audit.details["amount_micro_rub"] == 250_000_000
    assert audit.details["reason"] == "Тестовая запись аудита"
    assert audit.details["new_balance_micro_rub"] == 250_000_000
