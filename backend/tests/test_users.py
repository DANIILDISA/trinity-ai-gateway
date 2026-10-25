import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AdminAuditLog
from app.models.transaction import Transaction
from app.models.user import User
from app.services.billing_service import credit_balance

REGISTER_URL = "/api/v1/auth/register"
ME_URL = "/api/v1/users/me"
TXNS_URL = "/api/v1/users/me/transactions"

_TEST_EMAILS = [
    "test_me@example.com",
    "test_me_credit@example.com",
    "test_me_reserved@example.com",
    "test_me_txns@example.com",
    "test_me_page@example.com",
    "test_me_patch@example.com",
    "test_me_blocked@example.com",
]

_BASE_PAYLOAD = {
    "password": "SecurePass1",
    "display_name": "Тест",
}


@pytest_asyncio.fixture(autouse=True)
async def clean_test_users(db_session: AsyncSession) -> None:
    for email in _TEST_EMAILS:
        existing = (
            await db_session.execute(
                select(User).where(User.email_lower == email.lower())
            )
        ).scalar_one_or_none()
        if existing is not None:
            await db_session.execute(
                sa_delete(Transaction).where(Transaction.user_id == existing.id)
            )
            await db_session.execute(
                sa_delete(AdminAuditLog).where(AdminAuditLog.actor_id == existing.id)
            )
            await db_session.delete(existing)
    await db_session.commit()


async def _register(http_client: AsyncClient, email: str) -> tuple[str, str]:
    """Register a user and return (access_token, user_id)."""
    resp = await http_client.post(REGISTER_URL, json={**_BASE_PAYLOAD, "email": email})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return data["access_token"], data["user"]["id"]


async def _seed_credit(
    db_session: AsyncSession, user_id: str, amount: int, key: str
) -> None:
    import uuid as _uuid

    await credit_balance(
        db_session,
        user_id=_uuid.UUID(user_id),
        amount_micro_rub=amount,
        idempotency_key=key,
        description="Test top-up",
    )
    await db_session.commit()


async def test_get_me_returns_zero_balance_for_new_user(
    http_client: AsyncClient,
) -> None:
    token, _ = await _register(http_client, "test_me@example.com")

    resp = await http_client.get(
        ME_URL, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "test_me@example.com"
    assert data["display_name"] == "Тест"
    assert data["role"] == "user"
    assert data["status"] == "active"
    assert data["balance"] == "0.0000"
    assert "id" in data


async def test_get_me_reflects_credited_balance(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, user_id = await _register(http_client, "test_me_credit@example.com")
    await _seed_credit(db_session, user_id, 500_000_000, "test-me-credit-1")

    resp = await http_client.get(
        ME_URL, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["balance"] == "500.0000"


async def test_get_me_returns_reserved_and_available(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    """tr-tl3q: /users/me exposes held funds (reserved) and the spendable
    available = balance - reserved, so the UI can explain why "available" can dip
    below the headline balance during an in-flight request."""
    email = "test_me_reserved@example.com"
    token, user_id = await _register(http_client, email)
    await _seed_credit(db_session, user_id, 12_000_000, "test-me-reserved-1")
    # Hold part of the balance, as an in-flight reservation would.
    user = (
        await db_session.execute(select(User).where(User.email_lower == email.lower()))
    ).scalar_one()
    user.reserved_micro_rub = 5_000_000
    await db_session.commit()

    resp = await http_client.get(
        ME_URL, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["balance"] == "12.0000"
    assert data["reserved"] == "5.0000"
    assert data["available"] == "7.0000"


async def test_get_transactions_returns_correct_fields(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, user_id = await _register(http_client, "test_me_txns@example.com")
    await _seed_credit(db_session, user_id, 250_000_000, "test-me-txns-1")

    resp = await http_client.get(
        TXNS_URL, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["page"] == 1
    assert data["per_page"] == 20
    assert data["pages"] == 1
    assert len(data["items"]) == 1

    item = data["items"][0]
    assert item["type"] == "credit"
    assert item["amount"] == "250.0000"
    assert item["balance_after"] == "250.0000"
    assert item["description"] == "Test top-up"
    assert "id" in item
    assert "created_at" in item


async def test_get_transactions_pagination(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, user_id = await _register(http_client, "test_me_page@example.com")

    # Seed 25 credits. Each is 1_000_000 μ₽ = "1.0000" so amounts differ
    # only by transaction order — and we identify them by idempotency_key
    # via description so we can verify ordering.
    for i in range(1, 26):
        import uuid as _uuid
        await credit_balance(
            db_session,
            user_id=_uuid.UUID(user_id),
            amount_micro_rub=1_000_000,
            idempotency_key=f"page-{i}",
            description=f"credit-{i:02d}",
        )
        await db_session.commit()

    # Page 1, per_page=10 → newest first → credit-25 .. credit-16
    resp1 = await http_client.get(
        TXNS_URL,
        params={"page": 1, "per_page": 10},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp1.status_code == 200
    d1 = resp1.json()
    assert d1["total"] == 25
    assert d1["page"] == 1
    assert d1["per_page"] == 10
    assert d1["pages"] == 3
    assert len(d1["items"]) == 10
    descriptions_p1 = [it["description"] for it in d1["items"]]
    assert descriptions_p1[0] == "credit-25"
    assert descriptions_p1[-1] == "credit-16"

    # Page 2, per_page=10 → credit-15 .. credit-06
    resp2 = await http_client.get(
        TXNS_URL,
        params={"page": 2, "per_page": 10},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 200
    d2 = resp2.json()
    assert d2["page"] == 2
    assert len(d2["items"]) == 10
    descriptions_p2 = [it["description"] for it in d2["items"]]
    assert descriptions_p2[0] == "credit-15"
    assert descriptions_p2[-1] == "credit-06"

    # Page 3, per_page=10 → credit-05 .. credit-01
    resp3 = await http_client.get(
        TXNS_URL,
        params={"page": 3, "per_page": 10},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp3.status_code == 200
    d3 = resp3.json()
    assert d3["page"] == 3
    assert len(d3["items"]) == 5
    descriptions_p3 = [it["description"] for it in d3["items"]]
    assert descriptions_p3[0] == "credit-05"
    assert descriptions_p3[-1] == "credit-01"


async def test_patch_me_updates_display_name(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, user_id = await _register(http_client, "test_me_patch@example.com")

    resp = await http_client.patch(
        ME_URL,
        json={"display_name": "Новое имя"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Новое имя"

    # Verify persisted in DB
    import uuid as _uuid
    db_session.expire_all()
    user = (
        await db_session.execute(
            select(User).where(User.id == _uuid.UUID(user_id))
        )
    ).scalar_one()
    assert user.display_name == "Новое имя"

    # Subsequent GET reflects new name
    resp_get = await http_client.get(
        ME_URL, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp_get.json()["display_name"] == "Новое имя"


async def test_blocked_user_can_still_get_me(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, user_id = await _register(http_client, "test_me_blocked@example.com")

    # Block the user directly in DB
    import uuid as _uuid
    user = (
        await db_session.execute(
            select(User).where(User.id == _uuid.UUID(user_id))
        )
    ).scalar_one()
    user.status = "blocked"
    user.block_reason = "Test block"
    await db_session.commit()

    resp = await http_client.get(
        ME_URL, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "blocked"
    assert data["email"] == "test_me_blocked@example.com"
