"""Conversation CRUD endpoint tests (Task 6.1)."""
import asyncio
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AdminAuditLog
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.notification import Notification
from app.models.user import User

REGISTER_URL = "/api/v1/auth/register"
CONV_URL = "/api/v1/chat/conversations"
MODEL_ID = "openai/gpt-4.1-mini"

_TEST_EMAILS = [
    "convo_test_a@example.com",
    "convo_test_b@example.com",
]


@pytest_asyncio.fixture(autouse=True)
async def _clean_users(db_session: AsyncSession) -> None:
    """Wipe leftover state from previous runs so tests are independent."""
    for email in _TEST_EMAILS:
        user = (
            await db_session.execute(
                select(User).where(User.email_lower == email.lower())
            )
        ).scalar_one_or_none()
        if user is None:
            continue
        await db_session.execute(
            sa_delete(Message).where(
                Message.conversation_id.in_(
                    select(Conversation.id).where(Conversation.user_id == user.id)
                )
            )
        )
        await db_session.execute(
            sa_delete(Conversation).where(Conversation.user_id == user.id)
        )
        await db_session.execute(
            sa_delete(Notification).where(Notification.user_id == user.id)
        )
        await db_session.execute(
            sa_delete(AdminAuditLog).where(AdminAuditLog.actor_id == user.id)
        )
        await db_session.delete(user)
    await db_session.commit()


async def _register(http_client: AsyncClient, email: str) -> tuple[str, str]:
    resp = await http_client.post(
        REGISTER_URL,
        json={"email": email, "password": "SecurePass1", "display_name": "Тест"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["access_token"], body["user"]["id"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── 1. POST creates conversation ────────────────────────────────────────────


async def test_create_conversation_returns_correct_fields(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, user_id = await _register(http_client, "convo_test_a@example.com")

    resp = await http_client.post(
        CONV_URL,
        json={"model_id": MODEL_ID, "title": "Привет"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model_id"] == MODEL_ID
    assert body["title"] == "Привет"
    assert "id" in body and "created_at" in body and "updated_at" in body
    # POST should NOT include messages field per task spec.
    assert "messages" not in body

    # Ownership: row exists with the registering user as owner.
    db_session.expire_all()
    conv = (
        await db_session.execute(
            select(Conversation).where(Conversation.id == _uuid.UUID(body["id"]))
        )
    ).scalar_one()
    assert str(conv.user_id) == user_id
    assert conv.deleted_at is None


async def test_get_detail_returns_last_used_settings(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    """GET /conversations/{id} surfaces the last-used generation posture
    (migration 020 / tr-hsg.12) so reopening a chat restores its toggles. Fresh
    conversations have NULLs (frontend keeps defaults); once recorded, the values
    round-trip."""
    token, _ = await _register(http_client, "convo_test_a@example.com")
    conv_id = (
        await http_client.post(
            CONV_URL,
            json={"model_id": MODEL_ID, "title": "Настройки"},
            headers=_auth(token),
        )
    ).json()["id"]

    fresh = (
        await http_client.get(f"{CONV_URL}/{conv_id}", headers=_auth(token))
    ).json()
    assert fresh["last_thinking_enabled"] is None
    assert fresh["last_thinking_effort"] is None
    assert fresh["last_tools_enabled"] is None
    assert fresh["last_web_search_enabled"] is None

    # Simulate a request having recorded its posture on the conversation row.
    conv = (
        await db_session.execute(
            select(Conversation).where(Conversation.id == _uuid.UUID(conv_id))
        )
    ).scalar_one()
    conv.last_thinking_enabled = True
    conv.last_thinking_effort = "high"
    conv.last_tools_enabled = True
    conv.last_web_search_enabled = False
    await db_session.commit()

    restored = (
        await http_client.get(f"{CONV_URL}/{conv_id}", headers=_auth(token))
    ).json()
    assert restored["last_thinking_enabled"] is True
    assert restored["last_thinking_effort"] == "high"
    assert restored["last_tools_enabled"] is True
    assert restored["last_web_search_enabled"] is False


async def test_create_conversation_without_title(
    http_client: AsyncClient,
) -> None:
    token, _ = await _register(http_client, "convo_test_a@example.com")
    resp = await http_client.post(
        CONV_URL, json={"model_id": MODEL_ID}, headers=_auth(token)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Optional title — when omitted the response should not include it as a key.
    assert "title" not in body or body.get("title") is None


# ── 2. GET list ordering, soft-delete exclusion, pagination ────────────────


async def test_list_orders_newest_first_and_paginates(
    http_client: AsyncClient,
) -> None:
    token, _ = await _register(http_client, "convo_test_a@example.com")

    # Create 5 conversations with a small gap so updated_at orders deterministically.
    created_ids: list[str] = []
    for i in range(5):
        resp = await http_client.post(
            CONV_URL,
            json={"model_id": MODEL_ID, "title": f"#{i}"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        created_ids.append(resp.json()["id"])
        await asyncio.sleep(0.01)  # ensure distinct updated_at timestamps

    # page=1, per_page=2 → newest 2 first (the last two we created).
    resp = await http_client.get(
        f"{CONV_URL}?page=1&per_page=2", headers=_auth(token)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert body["page"] == 1
    assert body["per_page"] == 2
    assert len(body["items"]) == 2
    # No messages field on list items.
    assert all("messages" not in item for item in body["items"])
    # Newest first.
    assert [item["id"] for item in body["items"]] == [created_ids[4], created_ids[3]]

    # page=3 with per_page=2 → only the oldest (1 item).
    resp = await http_client.get(
        f"{CONV_URL}?page=3&per_page=2", headers=_auth(token)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == created_ids[0]


async def test_list_excludes_soft_deleted(
    http_client: AsyncClient,
) -> None:
    token, _ = await _register(http_client, "convo_test_a@example.com")

    a = (await http_client.post(
        CONV_URL, json={"model_id": MODEL_ID, "title": "A"}, headers=_auth(token)
    )).json()
    b = (await http_client.post(
        CONV_URL, json={"model_id": MODEL_ID, "title": "B"}, headers=_auth(token)
    )).json()

    # Soft-delete B.
    del_resp = await http_client.delete(
        f"{CONV_URL}/{b['id']}", headers=_auth(token)
    )
    assert del_resp.status_code == 204

    list_resp = await http_client.get(CONV_URL, headers=_auth(token))
    ids = [item["id"] for item in list_resp.json()["items"]]
    assert a["id"] in ids
    assert b["id"] not in ids
    assert list_resp.json()["total"] == 1


# ── 3. GET detail returns messages list ────────────────────────────────────


async def test_get_detail_returns_empty_messages_when_none(
    http_client: AsyncClient,
) -> None:
    token, _ = await _register(http_client, "convo_test_a@example.com")
    create = await http_client.post(
        CONV_URL, json={"model_id": MODEL_ID, "title": "Пусто"}, headers=_auth(token)
    )
    conv_id = create.json()["id"]

    resp = await http_client.get(f"{CONV_URL}/{conv_id}", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == conv_id
    assert body["messages"] == []


async def test_get_detail_returns_messages_in_created_at_order(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, _ = await _register(http_client, "convo_test_a@example.com")
    create = await http_client.post(
        CONV_URL, json={"model_id": MODEL_ID, "title": "Беседа"}, headers=_auth(token)
    )
    conv_id = _uuid.UUID(create.json()["id"])

    # Insert messages directly (chat-send isn't part of this task). Set explicit
    # created_at timestamps — Postgres now() returns transaction-start time for
    # both inserts otherwise, leaving ordering down to random UUID tiebreak.
    base_ts = datetime.now(timezone.utc)
    db_session.add(Message(
        conversation_id=conv_id,
        role="user",
        content="Привет",
        created_at=base_ts,
    ))
    db_session.add(Message(
        conversation_id=conv_id,
        role="assistant",
        content="Привет!",
        model_id=MODEL_ID,
        input_tokens=2,
        output_tokens=2,
        cost_micro_rub=123,
        request_id=_uuid.uuid4(),
        created_at=base_ts + timedelta(milliseconds=10),
    ))
    await db_session.commit()

    resp = await http_client.get(f"{CONV_URL}/{conv_id}", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant"]
    assistant = body["messages"][1]
    assert assistant["cost_micro_rub"] == 123
    assert assistant["model_id"] == MODEL_ID


# ── 4. PATCH renames ────────────────────────────────────────────────────────


async def test_patch_renames_and_bumps_updated_at(
    http_client: AsyncClient,
) -> None:
    token, _ = await _register(http_client, "convo_test_a@example.com")
    create = await http_client.post(
        CONV_URL, json={"model_id": MODEL_ID, "title": "Старое"}, headers=_auth(token)
    )
    conv_id = create.json()["id"]
    original_updated_at = create.json()["updated_at"]

    # Brief delay so updated_at provably advances.
    await asyncio.sleep(0.05)

    resp = await http_client.patch(
        f"{CONV_URL}/{conv_id}",
        json={"title": "Новое"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Новое"
    assert body["updated_at"] > original_updated_at
    # PATCH should not include messages.
    assert "messages" not in body


# ── 5. DELETE soft-deletes, subsequent GET → 404 ───────────────────────────


async def test_delete_returns_204_and_subsequent_get_404(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, _ = await _register(http_client, "convo_test_a@example.com")
    create = await http_client.post(
        CONV_URL, json={"model_id": MODEL_ID, "title": "Удалю"}, headers=_auth(token)
    )
    conv_id = create.json()["id"]

    del_resp = await http_client.delete(
        f"{CONV_URL}/{conv_id}", headers=_auth(token)
    )
    assert del_resp.status_code == 204
    # 204 No Content has empty body.
    assert del_resp.content == b""

    get_resp = await http_client.get(f"{CONV_URL}/{conv_id}", headers=_auth(token))
    assert get_resp.status_code == 404

    # Row still exists in DB but with deleted_at set (soft-delete).
    db_session.expire_all()
    conv = (
        await db_session.execute(
            select(Conversation).where(Conversation.id == _uuid.UUID(conv_id))
        )
    ).scalar_one()
    assert conv.deleted_at is not None


async def test_delete_twice_returns_404_second_time(
    http_client: AsyncClient,
) -> None:
    token, _ = await _register(http_client, "convo_test_a@example.com")
    create = await http_client.post(
        CONV_URL, json={"model_id": MODEL_ID, "title": "X"}, headers=_auth(token)
    )
    conv_id = create.json()["id"]

    first = await http_client.delete(f"{CONV_URL}/{conv_id}", headers=_auth(token))
    assert first.status_code == 204
    second = await http_client.delete(f"{CONV_URL}/{conv_id}", headers=_auth(token))
    assert second.status_code == 404


# ── 6. Cross-user isolation ────────────────────────────────────────────────


async def test_cross_user_isolation_returns_404(
    http_client: AsyncClient,
) -> None:
    token_a, _ = await _register(http_client, "convo_test_a@example.com")
    token_b, _ = await _register(http_client, "convo_test_b@example.com")

    create = await http_client.post(
        CONV_URL, json={"model_id": MODEL_ID, "title": "A"}, headers=_auth(token_a)
    )
    conv_id = create.json()["id"]

    # User B attempts each operation — all must be 404, not 403.
    get_resp = await http_client.get(f"{CONV_URL}/{conv_id}", headers=_auth(token_b))
    assert get_resp.status_code == 404

    patch_resp = await http_client.patch(
        f"{CONV_URL}/{conv_id}",
        json={"title": "взлом"},
        headers=_auth(token_b),
    )
    assert patch_resp.status_code == 404

    del_resp = await http_client.delete(
        f"{CONV_URL}/{conv_id}", headers=_auth(token_b)
    )
    assert del_resp.status_code == 404

    # B's list must be empty (not include A's conversation).
    list_b = await http_client.get(CONV_URL, headers=_auth(token_b))
    assert list_b.json()["total"] == 0

    # The conversation still exists for A.
    get_a = await http_client.get(f"{CONV_URL}/{conv_id}", headers=_auth(token_a))
    assert get_a.status_code == 200
