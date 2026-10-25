"""OpenRouter API key admin endpoints + encryption round-trip tests."""
import uuid as _uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_key, encrypt_key
from app.models.api_key import ApiKey
from app.models.audit_log import AdminAuditLog
from app.models.user import User
from app.services.openrouter_proxy import get_active_key

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
KEYS_URL = "/api/v1/admin/keys"

_TEST_EMAIL = "admin_keys_actor@example.com"


@pytest_asyncio.fixture(autouse=True)
async def reset_keys_state(db_session: AsyncSession) -> None:
    # Clear all api_keys rows and the test admin user.
    await db_session.execute(sa_delete(ApiKey))
    existing = (
        await db_session.execute(
            select(User).where(User.email_lower == _TEST_EMAIL.lower())
        )
    ).scalar_one_or_none()
    if existing is not None:
        await db_session.execute(
            sa_delete(AdminAuditLog).where(AdminAuditLog.actor_id == existing.id)
        )
        await db_session.delete(existing)
    await db_session.commit()


async def _register_admin(
    http_client: AsyncClient, db_session: AsyncSession
) -> str:
    resp = await http_client.post(
        REGISTER_URL,
        json={
            "email": _TEST_EMAIL,
            "password": "SecurePass1",
            "display_name": "Админ",
        },
    )
    assert resp.status_code == 200, resp.text
    user_id = resp.json()["user"]["id"]
    user = (
        await db_session.execute(
            select(User).where(User.id == _uuid.UUID(user_id))
        )
    ).scalar_one()
    user.role = "admin"
    await db_session.commit()
    login = await http_client.post(
        LOGIN_URL,
        json={"email": _TEST_EMAIL, "password": "SecurePass1"},
    )
    assert login.status_code == 200
    return login.json()["access_token"]


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_add_key_stores_encrypted(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _register_admin(http_client, db_session)
    plaintext = "sk-or-v1-1234567890abcdef"
    resp = await http_client.post(
        KEYS_URL,
        json={"name": "primary", "key": plaintext},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["key_suffix"] == plaintext[-8:]
    assert body["status"] == "active"

    key_row = (
        await db_session.execute(
            select(ApiKey).where(ApiKey.id == _uuid.UUID(body["id"]))
        )
    ).scalar_one()
    assert key_row.encrypted_key != plaintext
    assert plaintext not in key_row.encrypted_key  # not stored verbatim
    assert key_row.key_suffix == plaintext[-8:]
    # Round-trip decryption returns the original.
    assert decrypt_key(key_row.encrypted_key) == plaintext


async def test_list_keys_no_plaintext(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _register_admin(http_client, db_session)
    plaintext = "sk-or-v1-listme-XYZ12345"
    await http_client.post(
        KEYS_URL,
        json={"name": "alpha", "key": plaintext},
        headers={"Authorization": f"Bearer {token}"},
    )

    resp = await http_client.get(
        KEYS_URL, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    item = items[0]
    assert "encrypted_key" not in item
    assert "key" not in item
    assert plaintext not in str(item)
    assert item["key_suffix"] == plaintext[-8:]


async def test_get_active_key_decrypts(db_session: AsyncSession) -> None:
    plaintext = "sk-or-v1-roundtrip-ABCDEFGH"
    db_session.add(ApiKey(
        label="primary",
        encrypted_key=encrypt_key(plaintext),
        key_suffix=plaintext[-8:],
        status="active",
        is_primary=True,
    ))
    await db_session.commit()

    got = await get_active_key(db_session)
    assert got == plaintext


async def test_activate_deactivates_previous(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _register_admin(http_client, db_session)

    # Create two keys.
    raw_a = "sk-or-v1-aaaaaaaa"
    raw_b = "sk-or-v1-bbbbbbbb"
    resp_a = await http_client.post(
        KEYS_URL, json={"name": "A", "key": raw_a},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp_b = await http_client.post(
        KEYS_URL, json={"name": "B", "key": raw_b},
        headers={"Authorization": f"Bearer {token}"},
    )
    id_a = resp_a.json()["id"]
    id_b = resp_b.json()["id"]

    # Activate A first.
    await http_client.post(
        f"{KEYS_URL}/{id_a}/activate",
        headers={"Authorization": f"Bearer {token}"},
    )
    # Then activate B.
    activate_b = await http_client.post(
        f"{KEYS_URL}/{id_b}/activate",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert activate_b.status_code == 200
    assert activate_b.json()["is_primary"] is True

    db_session.expire_all()
    row_a = (
        await db_session.execute(select(ApiKey).where(ApiKey.id == _uuid.UUID(id_a)))
    ).scalar_one()
    row_b = (
        await db_session.execute(select(ApiKey).where(ApiKey.id == _uuid.UUID(id_b)))
    ).scalar_one()
    assert row_a.is_primary is False
    assert row_b.is_primary is True


async def test_disable_primary_key(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _register_admin(http_client, db_session)
    raw = "sk-or-v1-disable-ZZZZZZZZ"
    resp = await http_client.post(
        KEYS_URL, json={"name": "primary", "key": raw},
        headers={"Authorization": f"Bearer {token}"},
    )
    key_id = resp.json()["id"]
    await http_client.post(
        f"{KEYS_URL}/{key_id}/activate",
        headers={"Authorization": f"Bearer {token}"},
    )

    # Sanity: active key resolves.
    assert await get_active_key(db_session) == raw

    # Now disable it.
    patch = await http_client.patch(
        f"{KEYS_URL}/{key_id}",
        json={"status": "disabled"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert patch.status_code == 200
    assert patch.json()["status"] == "disabled"
    # Per task: is_primary stays True, status becomes 'disabled'.
    assert patch.json()["is_primary"] is True

    db_session.expire_all()
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await get_active_key(db_session)
    assert exc_info.value.status_code == 503


async def test_get_active_key_skips_blocked_account(
    db_session: AsyncSession,
) -> None:
    """A primary+active key whose account is blocked must never be selected."""
    plaintext = "sk-or-v1-blocked-ACCOUNT1"
    db_session.add(ApiKey(
        label="banned",
        encrypted_key=encrypt_key(plaintext),
        key_suffix=plaintext[-8:],
        status="active",
        is_primary=True,
        account_name="acct-banned",
        account_blocked=True,
    ))
    await db_session.commit()

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await get_active_key(db_session)
    assert exc_info.value.status_code == 503


async def test_block_account_affects_all_keys(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _register_admin(http_client, db_session)

    # Two keys under the same account, one under a different account.
    for raw, account in (
        ("sk-or-v1-acctA-1111", "acct-A"),
        ("sk-or-v1-acctA-2222", "acct-A"),
        ("sk-or-v1-acctB-3333", "acct-B"),
    ):
        resp = await http_client.post(
            KEYS_URL,
            json={"name": account, "key": raw, "account_name": account},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

    block = await http_client.post(
        f"{KEYS_URL}/accounts/acct-A/block",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert block.status_code == 200, block.text
    assert block.json()["affected_keys"] == 2

    db_session.expire_all()
    rows = (
        await db_session.execute(select(ApiKey).order_by(ApiKey.account_name))
    ).scalars().all()
    by_account: dict[str, list[bool]] = {}
    for r in rows:
        by_account.setdefault(r.account_name, []).append(r.account_blocked)
    assert all(by_account["acct-A"])  # both acct-A keys blocked
    assert not any(by_account["acct-B"])  # acct-B untouched

    # Unblock restores all acct-A keys.
    unblock = await http_client.post(
        f"{KEYS_URL}/accounts/acct-A/unblock",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert unblock.status_code == 200
    assert unblock.json()["affected_keys"] == 2
    db_session.expire_all()
    rows2 = (
        await db_session.execute(
            select(ApiKey).where(ApiKey.account_name == "acct-A")
        )
    ).scalars().all()
    assert not any(r.account_blocked for r in rows2)


async def test_initial_key_auto_created(
    http_client: AsyncClient,  # triggers init_engine for the global session factory
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate startup: when api_keys is empty and the env var is set, the
    lifespan hook seeds a primary 'default' key."""
    from app.config import settings as live_settings
    from app.main import _seed_initial_api_key

    raw = "sk-or-v1-initialBOOT123"
    monkeypatch.setattr(live_settings, "openrouter_default_api_key", raw)

    # Ensure table is empty (autouse fixture already cleared it).
    count = (await db_session.execute(select(ApiKey))).scalars().all()
    assert count == []

    await _seed_initial_api_key()

    db_session.expire_all()
    rows = (await db_session.execute(select(ApiKey))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.label == "default"
    assert row.is_primary is True
    assert row.status == "active"
    assert row.key_suffix == raw[-8:]
    assert decrypt_key(row.encrypted_key) == raw

    # Running again is a no-op (does not insert a second row).
    await _seed_initial_api_key()
    db_session.expire_all()
    rows2 = (await db_session.execute(select(ApiKey))).scalars().all()
    assert len(rows2) == 1
