"""YooKassa payment-creation and webhook tests.

All HTTP calls to YooKassa are mocked at the httpx.AsyncClient.post level —
no real credentials needed. Webhook signatures are computed locally using
the configured YOOKASSA_SECRET_KEY (empty string in tests).
"""
import hashlib
import hmac
import json
import uuid as _uuid

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.alert import Alert
from app.models.audit_log import AdminAuditLog
from app.models.notification import Notification
from app.models.transaction import Transaction
from app.models.user import User

REGISTER_URL = "/api/v1/auth/register"
CREATE_URL = "/api/v1/payments/create"
WEBHOOK_URL = "/api/v1/payments/webhook"

_TEST_EMAILS = [
    "pay_ok@example.com",
    "pay_below@example.com",
    "pay_blocked@example.com",
    "pay_params@example.com",
    "pay_unauth@example.com",
    "pay_hook_ok@example.com",
    "pay_hook_dup@example.com",
    "pay_hook_invalid@example.com",
    "pay_hook_nojwt@example.com",
    "pay_hook_pending@example.com",
    "pay_status_ok@example.com",
    "pay_status_wrong_a@example.com",
    "pay_status_wrong_b@example.com",
    "pay_status_404@example.com",
]

_REGISTER_PAYLOAD = {
    "password": "SecurePass1",
    "display_name": "Платёжник",
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
                sa_delete(Notification).where(Notification.user_id == existing.id)
            )
            await db_session.execute(
                sa_delete(Transaction).where(Transaction.user_id == existing.id)
            )
            await db_session.execute(
                sa_delete(AdminAuditLog).where(AdminAuditLog.actor_id == existing.id)
            )
            await db_session.delete(existing)
    # Wipe any "Invalid webhook signature" alerts from prior runs so the count
    # assertion in test_webhook_invalid_signature is deterministic.
    await db_session.execute(
        sa_delete(Alert).where(Alert.title == "Invalid webhook signature")
    )
    await db_session.commit()


def _sign(raw: bytes) -> str:
    return hmac.new(
        settings.yookassa_secret_key.encode(), raw, hashlib.sha256
    ).hexdigest()


def _build_webhook_body(
    user_id: str,
    payment_id: str,
    amount_value: str = "500.00",
    status: str = "succeeded",
) -> bytes:
    return json.dumps({
        "event": f"payment.{status}",
        "object": {
            "id": payment_id,
            "status": status,
            "amount": {"value": amount_value, "currency": "RUB"},
            "metadata": {"user_id": user_id},
        },
    }).encode()


class _FakeResponse:
    def __init__(self, data: dict, status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _install_yookassa_get_mock(
    monkeypatch,
    response_data: dict | None = None,
    status_code: int = 200,
) -> list[dict]:
    """Patch httpx.AsyncClient.get to intercept only YooKassa calls."""
    calls: list[dict] = []
    default_response = response_data if response_data is not None else {
        "id": "yk-status-default",
        "status": "succeeded",
        "amount": {"value": "500.00", "currency": "RUB"},
        "metadata": {"user_id": "00000000-0000-0000-0000-000000000000"},
    }

    original_get = httpx.AsyncClient.get

    async def _fake_get(self, url, **kwargs):
        if str(url).startswith("https://api.yookassa.ru"):
            calls.append({"url": str(url), **kwargs})
            return _FakeResponse(default_response, status_code=status_code)
        return await original_get(self, url, **kwargs)

    monkeypatch.setattr("httpx.AsyncClient.get", _fake_get)
    return calls


def _install_yookassa_mock(monkeypatch, response_data: dict | None = None) -> list[dict]:
    """Patch httpx.AsyncClient.post to intercept only YooKassa calls.

    Non-YooKassa URLs (i.e., the test client's calls into FastAPI's ASGI app)
    pass through to the original method so they reach the real handler.
    """
    calls: list[dict] = []
    default_response = response_data or {
        "id": "yookassa-payment-abc123",
        "status": "pending",
        "amount": {"value": "500.00", "currency": "RUB"},
        "confirmation": {
            "type": "redirect",
            "confirmation_url": "https://yoomoney.ru/checkout/xyz",
        },
    }

    original_post = httpx.AsyncClient.post

    async def _fake_post(self, url, **kwargs):
        if str(url).startswith("https://api.yookassa.ru"):
            calls.append({"url": str(url), **kwargs})
            return _FakeResponse(default_response)
        return await original_post(self, url, **kwargs)

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)
    return calls


async def _register(http_client: AsyncClient, email: str) -> tuple[str, str]:
    resp = await http_client.post(REGISTER_URL, json={**_REGISTER_PAYLOAD, "email": email})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return data["access_token"], data["user"]["id"]


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_create_payment_success(
    http_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, _ = await _register(http_client, "pay_ok@example.com")
    _install_yookassa_mock(monkeypatch)

    resp = await http_client.post(
        CREATE_URL,
        json={"amount": 500},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["payment_id"] == "yookassa-payment-abc123"
    assert data["confirmation_url"] == "https://yoomoney.ru/checkout/xyz"
    assert data["status"] == "pending"
    assert data["amount"] == "500.00"


async def test_create_payment_below_minimum(
    http_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, _ = await _register(http_client, "pay_below@example.com")
    calls = _install_yookassa_mock(monkeypatch)

    resp = await http_client.post(
        CREATE_URL,
        json={"amount": 499},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    # YooKassa should not have been called.
    assert calls == []


async def test_create_payment_blocked_user(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid as _uuid

    token, user_id = await _register(http_client, "pay_blocked@example.com")
    user = (
        await db_session.execute(
            select(User).where(User.id == _uuid.UUID(user_id))
        )
    ).scalar_one()
    user.status = "blocked"
    user.block_reason = "Test block"
    await db_session.commit()

    calls = _install_yookassa_mock(monkeypatch)
    resp = await http_client.post(
        CREATE_URL,
        json={"amount": 500},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert calls == []


async def test_create_payment_yookassa_called_with_correct_params(
    http_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, user_id = await _register(http_client, "pay_params@example.com")
    calls = _install_yookassa_mock(monkeypatch)

    resp = await http_client.post(
        CREATE_URL,
        json={"amount": 750},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://api.yookassa.ru/v3/payments"

    body = call["json"]
    assert body["method"] == {"type": "sbp"}
    assert body["amount"] == {"value": "750.00", "currency": "RUB"}
    assert body["confirmation"]["type"] == "redirect"
    assert "return_url" in body["confirmation"]
    assert body["metadata"] == {"user_id": user_id}

    # Idempotency-Key header is present and parseable as a UUID-ish string.
    headers = call["headers"]
    assert "Idempotency-Key" in headers
    assert len(headers["Idempotency-Key"]) > 0

    # HTTP Basic auth uses the configured shop credentials (settings default to empty strings in tests).
    assert "auth" in call


async def test_create_payment_unauthenticated(
    http_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_yookassa_mock(monkeypatch)

    resp = await http_client.post(
        CREATE_URL,
        json={"amount": 500},
    )
    # No Authorization header → 401/403 from the auth dependency.
    assert resp.status_code in (401, 403)
    assert calls == []


# ── Webhook tests ────────────────────────────────────────────────────────────


async def test_webhook_valid_succeeds(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, user_id = await _register(http_client, "pay_hook_ok@example.com")
    payment_id = f"yk-test-{_uuid.uuid4()}"
    body = _build_webhook_body(user_id, payment_id, "500.00", "succeeded")

    resp = await http_client.post(
        WEBHOOK_URL,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-YooKassa-Signature": _sign(body),
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["duplicate"] is False

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == _uuid.UUID(user_id)))
    ).scalar_one()
    assert user.balance_micro_rub == 500_000_000

    txn = (
        await db_session.execute(
            select(Transaction).where(Transaction.payment_id == payment_id)
        )
    ).scalar_one()
    assert txn.type == "credit"
    assert txn.amount_micro_rub == 500_000_000
    assert txn.idempotency_key == payment_id

    note = (
        await db_session.execute(
            select(Notification).where(Notification.user_id == _uuid.UUID(user_id))
        )
    ).scalar_one()
    assert note.type == "topup_confirmed"
    assert note.title == "Баланс пополнен"
    assert "500.00" in note.message


async def test_webhook_duplicate(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, user_id = await _register(http_client, "pay_hook_dup@example.com")
    payment_id = f"yk-dup-{_uuid.uuid4()}"
    body = _build_webhook_body(user_id, payment_id, "500.00", "succeeded")
    sig = _sign(body)

    r1 = await http_client.post(
        WEBHOOK_URL,
        content=body,
        headers={"Content-Type": "application/json", "X-YooKassa-Signature": sig},
    )
    assert r1.status_code == 200
    assert r1.json()["duplicate"] is False

    r2 = await http_client.post(
        WEBHOOK_URL,
        content=body,
        headers={"Content-Type": "application/json", "X-YooKassa-Signature": sig},
    )
    assert r2.status_code == 200
    assert r2.json()["duplicate"] is True

    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == _uuid.UUID(user_id)))
    ).scalar_one()
    # Credit applied exactly once
    assert user.balance_micro_rub == 500_000_000

    txn_count = (
        await db_session.execute(
            select(Transaction).where(Transaction.payment_id == payment_id)
        )
    ).scalars().all()
    assert len(txn_count) == 1


async def test_webhook_invalid_signature(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, user_id = await _register(http_client, "pay_hook_invalid@example.com")
    body = _build_webhook_body(
        user_id, f"yk-bad-{_uuid.uuid4()}", "500.00", "succeeded"
    )

    resp = await http_client.post(
        WEBHOOK_URL,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-YooKassa-Signature": "deadbeef" * 8,  # wrong sig
        },
    )
    assert resp.status_code == 401

    # Balance untouched
    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == _uuid.UUID(user_id)))
    ).scalar_one()
    assert user.balance_micro_rub == 0

    # Alert was recorded
    alerts = (
        await db_session.execute(
            select(Alert).where(Alert.title == "Invalid webhook signature")
        )
    ).scalars().all()
    assert len(alerts) >= 1
    assert alerts[0].severity == "warning"
    assert alerts[0].source == "payments"


async def test_webhook_no_jwt_required(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, user_id = await _register(http_client, "pay_hook_nojwt@example.com")
    payment_id = f"yk-nojwt-{_uuid.uuid4()}"
    body = _build_webhook_body(user_id, payment_id, "500.00", "succeeded")
    sig = _sign(body)

    # Strip the Authorization header that the test client might otherwise carry
    # (the conftest fixture does not set one, but be explicit).
    resp = await http_client.post(
        WEBHOOK_URL,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-YooKassa-Signature": sig,
            "Authorization": "",  # explicitly empty
        },
    )
    assert resp.status_code == 200


async def test_webhook_non_succeeded_event(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, user_id = await _register(http_client, "pay_hook_pending@example.com")
    payment_id = f"yk-pending-{_uuid.uuid4()}"
    body = _build_webhook_body(user_id, payment_id, "500.00", "pending")
    sig = _sign(body)

    resp = await http_client.post(
        WEBHOOK_URL,
        content=body,
        headers={"Content-Type": "application/json", "X-YooKassa-Signature": sig},
    )
    assert resp.status_code == 200
    # No credit applied
    db_session.expire_all()
    user = (
        await db_session.execute(select(User).where(User.id == _uuid.UUID(user_id)))
    ).scalar_one()
    assert user.balance_micro_rub == 0

    # No transaction row
    txns = (
        await db_session.execute(
            select(Transaction).where(Transaction.payment_id == payment_id)
        )
    ).scalars().all()
    assert txns == []


# ── Status endpoint tests ────────────────────────────────────────────────────


async def test_payment_status_success(
    http_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, user_id = await _register(http_client, "pay_status_ok@example.com")
    payment_id = "yk-status-ok-1"

    calls = _install_yookassa_get_mock(monkeypatch, response_data={
        "id": payment_id,
        "status": "succeeded",
        "amount": {"value": "500.00", "currency": "RUB"},
        "metadata": {"user_id": user_id},
    })

    resp = await http_client.get(
        f"/api/v1/payments/{payment_id}/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["payment_id"] == payment_id
    assert data["status"] == "succeeded"
    assert data["amount"] == "500.00"

    assert len(calls) == 1
    assert calls[0]["url"] == f"https://api.yookassa.ru/v3/payments/{payment_id}"


async def test_payment_status_wrong_user(
    http_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_a, _ = await _register(http_client, "pay_status_wrong_a@example.com")
    _, user_b_id = await _register(http_client, "pay_status_wrong_b@example.com")
    payment_id = "yk-status-wrong-1"

    # Mock returns payment whose metadata says it belongs to user B.
    _install_yookassa_get_mock(monkeypatch, response_data={
        "id": payment_id,
        "status": "succeeded",
        "amount": {"value": "500.00", "currency": "RUB"},
        "metadata": {"user_id": user_b_id},
    })

    # User A asks for it → 404 (do not reveal that it exists)
    resp = await http_client.get(
        f"/api/v1/payments/{payment_id}/status",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 404


async def test_payment_status_not_found(
    http_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, _ = await _register(http_client, "pay_status_404@example.com")

    _install_yookassa_get_mock(monkeypatch, response_data={}, status_code=404)

    resp = await http_client.get(
        "/api/v1/payments/nonexistent-id/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
