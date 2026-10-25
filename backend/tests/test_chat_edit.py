"""POST /api/v1/chat/conversations/{id}/messages/{message_id}/edit (Task 7).

Editing an active user message: bills ZERO for the edit itself, exactly ONE
generation for the forced reply, carries the original message's attachments
over to the new sibling node, leaves the old branch's cost/request_id immutable
(no refund), and feeds the EDITED text (not the original) to the proxy via
append_user_turn=False against the active history (Task 0 §0.1/§0.3).
"""
import json
import uuid as _uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.api.v1.chat as chat_module
import app.services.openrouter_proxy as openrouter_proxy_module
from app.models.chat_file import ChatFile
from app.models.chat_image import ChatImage
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.model_pricing import ModelPricing
from app.models.notification import Notification
from app.models.reservation import Reservation
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services.branching_service import derive_version_info
from app.services.billing_service import credit_balance
from app.services.openrouter_proxy import StreamChunk

REGISTER_URL = "/api/v1/auth/register"
CONV_URL = "/api/v1/chat/conversations"
MODEL_ID = "openai/gpt-4.1-mini"

_TEST_EMAILS = ["edit_test_a@example.com"]
# 1x1 transparent PNG as a data URI.
_PNG_DATA_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
# A tiny text file as an OpenRouter `file` part (base64 of "hello edit").
_TXT_DATA_URI = "data:text/plain;base64,aGVsbG8gZWRpdA=="


@pytest_asyncio.fixture(autouse=True)
async def _clean_users(db_session: AsyncSession) -> None:
    for email in _TEST_EMAILS:
        user = (
            await db_session.execute(
                select(User).where(User.email_lower == email.lower())
            )
        ).scalar_one_or_none()
        if user is None:
            continue
        uid = user.id
        conv_ids = select(Conversation.id).where(Conversation.user_id == uid)
        await db_session.execute(sa_delete(ChatImage).where(ChatImage.user_id == uid))
        await db_session.execute(sa_delete(ChatFile).where(ChatFile.user_id == uid))
        await db_session.execute(
            sa_delete(Message).where(Message.conversation_id.in_(conv_ids))
        )
        await db_session.execute(sa_delete(Conversation).where(Conversation.user_id == uid))
        await db_session.execute(sa_delete(UsageLog).where(UsageLog.user_id == uid))
        await db_session.execute(sa_delete(Reservation).where(Reservation.user_id == uid))
        await db_session.execute(sa_delete(Transaction).where(Transaction.user_id == uid))
        await db_session.execute(sa_delete(Notification).where(Notification.user_id == uid))
        await db_session.delete(user)
    await db_session.commit()


@pytest_asyncio.fixture(autouse=True)
async def _clear_rate_limit(redis_client):
    keys = await redis_client.keys("ratelimit:*")
    if keys:
        await redis_client.delete(*keys)
    yield
    keys = await redis_client.keys("ratelimit:*")
    if keys:
        await redis_client.delete(*keys)


async def _register(http_client: AsyncClient, email: str) -> tuple[str, str]:
    resp = await http_client.post(
        REGISTER_URL,
        json={"email": email, "password": "SecurePass1", "display_name": "Чатер"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["access_token"], body["user"]["id"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_balance(db: AsyncSession, user_id_str: str, amount: int) -> None:
    await credit_balance(
        db,
        user_id=_uuid.UUID(user_id_str),
        amount_micro_rub=amount,
        idempotency_key=f"edit-seed-{user_id_str}-{_uuid.uuid4()}",
        description="edit test seed",
    )
    await db.commit()


async def _create_conversation(http_client: AsyncClient, token: str) -> str:
    resp = await http_client.post(
        CONV_URL,
        json={"model_id": MODEL_ID, "title": "Тест"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


# Captures the context messages handed to the proxy on each call so the
# context test can assert the edited text (not the original) reaches the model.
_LAST_CONTEXTS: list[list[dict]] = []
# Captures the reasoning_budget passed to the proxy on each call so the
# thinking-inheritance test can assert the edit requested reasoning.
_LAST_REASONING_BUDGETS: list[int | None] = []
# Captures the plugins passed to the proxy on each call so the web-search
# inheritance test can assert the edit forwarded the {"id": "web"} plugin.
_LAST_PLUGINS: list[list[dict] | None] = []
# A reasoning-capable model in trinity_test (supports_reasoning=true), needed to
# exercise the thinking gate end-to-end (gpt-4.1-mini has it disabled).
REASONING_MODEL_ID = "anthropic/claude-sonnet-4-6"


def _install_proxy(monkeypatch: pytest.MonkeyPatch, chunks) -> None:
    class _FakeProxy:
        def __init__(self, db):
            self.db = db

        async def proxy_to_openrouter_with_retry(
            self, messages, model_id, max_retries=3, plugins=None, reasoning_budget=None
        ):
            _LAST_CONTEXTS.append([dict(m) for m in messages])
            _LAST_REASONING_BUDGETS.append(reasoning_budget)
            _LAST_PLUGINS.append(plugins)
            for c in chunks:
                yield c

    monkeypatch.setattr(openrouter_proxy_module, "OpenRouterProxy", _FakeProxy)
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "openrouter_demo_mode", False)


def _parse_sse(text: str) -> list[dict]:
    events: list[dict] = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        ev: dict = {}
        for line in block.split("\n"):
            if line.startswith("event: "):
                ev["event"] = line[len("event: "):]
            elif line.startswith("data: "):
                ev["data"] = json.loads(line[len("data: "):])
        if ev:
            events.append(ev)
    return events


async def _send_first_turn(
    http_client: AsyncClient, token: str, conv_id: str, content
) -> None:
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages",
        json={"content": content, "model": MODEL_ID},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    # Drain the stream so the assistant message is persisted before we edit.
    assert _parse_sse(resp.text)[-1]["event"] == "message_done"


# ── Tests ────────────────────────────────────────────────────────────────────


def _install_pcr_recorder(monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    from app.services.billing_types import SSEEvent

    async def _fake_pcr(**kwargs):
        captured.clear()
        captured.update(kwargs)
        yield SSEEvent("message_start", {"request_id": str(_uuid.uuid4()), "estimated_cost": "0"})
        yield SSEEvent(
            "message_done",
            {
                "request_id": str(_uuid.uuid4()),
                "message_id": str(_uuid.uuid4()),
                "user_message_id": str(_uuid.uuid4()),
                "active_leaf_id": str(_uuid.uuid4()),
                "tokens_used": {"input": 1, "output": 1, "reasoning": 0},
            },
        )

    monkeypatch.setattr(chat_module, "process_chat_request", _fake_pcr)


async def test_edit_forwards_live_toggles(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tr-1hm: editAndResend must carry the live tools/thinking/web toggles into
    the forced reply (an explicit body value overrides the inherited posture)."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="ответ"),
        StreamChunk(type="usage", input_tokens=4, output_tokens=2),
        StreamChunk(type="done"),
    ])
    await _send_first_turn(http_client, token, conv_id, "Привет")
    db_session.expire_all()
    user_msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "user",
            )
        )
    ).scalar_one()

    captured: dict = {}
    _install_pcr_recorder(monkeypatch, captured)
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{user_msg.id}/edit",
        json={
            "content": "Привет, изменённый",
            "tools_enabled": True,
            "thinking_enabled": True,
            "web_search_enabled": True,
        },
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert captured.get("tools_enabled") is True
    assert captured.get("thinking_enabled") is True
    assert captured.get("web_search_enabled") is True


async def test_edit_uses_overridden_model(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tr-77u: editAndResend with an explicit `model` (the picker's CURRENT model,
    different from the conversation's stored model) must use that NEW model — both
    forwarded to process_chat_request AND persisted to conversation.model_id, so the
    forced reply is priced/recorded against the model the user just picked (parity
    with send/regenerate), not the stale model from the original send."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="ответ"),
        StreamChunk(type="usage", input_tokens=4, output_tokens=2),
        StreamChunk(type="done"),
    ])
    await _send_first_turn(http_client, token, conv_id, "Привет")
    db_session.expire_all()
    user_msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "user",
            )
        )
    ).scalar_one()

    new_model = REASONING_MODEL_ID
    assert new_model != MODEL_ID

    captured: dict = {}
    _install_pcr_recorder(monkeypatch, captured)
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{user_msg.id}/edit",
        json={"content": "Привет, изменённый", "model": new_model},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert captured.get("model_id") == new_model
    db_session.expire_all()
    conv = (
        await db_session.execute(
            select(Conversation).where(Conversation.id == _uuid.UUID(conv_id))
        )
    ).scalar_one()
    assert conv.model_id == new_model


async def test_edit_bills_zero_for_edit_and_one_generation(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The edit itself does NO model call / NO debit for the new user node;
    exactly ONE generation (the forced reply) is billed; the OLD branch's
    assistant cost_micro_rub + request_id are UNCHANGED (no refund). Balance
    delta == the new generation cost only."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)
    uid = _uuid.UUID(user_id)

    _LAST_CONTEXTS.clear()
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Первый ответ"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])
    await _send_first_turn(http_client, token, conv_id, "Привет")

    db_session.expire_all()
    user_msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "user",
            )
        )
    ).scalar_one()
    old_user_id = user_msg.id
    old_assistant = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "assistant",
            )
        )
    ).scalar_one()
    old_cost = old_assistant.cost_micro_rub
    old_request_id = old_assistant.request_id
    old_assistant_id = old_assistant.id
    assert old_cost is not None and old_request_id is not None

    balance_before_edit = (
        await db_session.execute(select(User.balance_micro_rub).where(User.id == uid))
    ).scalar_one()
    usage_count_before = (
        await db_session.execute(
            select(func.count()).select_from(UsageLog).where(UsageLog.user_id == uid)
        )
    ).scalar_one()

    # Edit the user message → exactly ONE new generation.
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Второй ответ"),
        StreamChunk(type="usage", input_tokens=8, output_tokens=4),
        StreamChunk(type="done"),
    ])
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{old_user_id}/edit",
        json={"content": "Привет, исправлено"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    done = next(e for e in events if e["event"] == "message_done")
    assert done["data"]["tokens_used"] == {"input": 8, "output": 4, "reasoning": 0}
    assert "message_id" in done["data"]
    assert "active_leaf_id" in done["data"]

    db_session.expire_all()
    # Exactly ONE additional usage_log (the generation); the edit bills nothing.
    usage_count_after = (
        await db_session.execute(
            select(func.count()).select_from(UsageLog).where(UsageLog.user_id == uid)
        )
    ).scalar_one()
    assert usage_count_after == usage_count_before + 1

    balance_after_edit = (
        await db_session.execute(select(User.balance_micro_rub).where(User.id == uid))
    ).scalar_one()
    new_assistant = (
        await db_session.execute(
            select(Message).where(Message.id == _uuid.UUID(done["data"]["message_id"]))
        )
    ).scalar_one()
    # Balance drops by exactly the new generation's debit — nothing for the edit.
    assert balance_before_edit - balance_after_edit == new_assistant.cost_micro_rub

    # The OLD branch's assistant cost + request_id are immutable (no refund).
    old_assistant_reloaded = (
        await db_session.execute(
            select(Message).where(Message.id == old_assistant_id)
        )
    ).scalar_one()
    assert old_assistant_reloaded.cost_micro_rub == old_cost
    assert old_assistant_reloaded.request_id == old_request_id
    assert old_assistant_reloaded.is_active is False
    assert old_assistant_reloaded.deleted_at is None


async def test_edit_creates_active_user_sibling(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new user node shares parent_id with the target, is_active=true, the
    target becomes is_active=false; version_count==2 for that user turn; the
    assistant child parents off the NEW user node; active_leaf_id == new
    assistant."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Первый ответ"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])
    await _send_first_turn(http_client, token, conv_id, "Привет")

    db_session.expire_all()
    target = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "user",
            )
        )
    ).scalar_one()
    target_id = target.id
    target_parent_id = target.parent_id

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Второй ответ"),
        StreamChunk(type="usage", input_tokens=8, output_tokens=4),
        StreamChunk(type="done"),
    ])
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{target_id}/edit",
        json={"content": "Привет, исправлено"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    done = next(e for e in _parse_sse(resp.text) if e["event"] == "message_done")

    db_session.expire_all()
    new_assistant = (
        await db_session.execute(
            select(Message).where(Message.id == _uuid.UUID(done["data"]["message_id"]))
        )
    ).scalar_one()
    new_user = (
        await db_session.execute(
            select(Message).where(Message.id == new_assistant.parent_id)
        )
    ).scalar_one()
    new_user_id = new_user.id
    assert new_user.role == "user"
    assert new_user.content == "Привет, исправлено"
    assert new_user.parent_id == target_parent_id  # sibling of the original
    assert new_user.is_active is True

    target_reloaded = (
        await db_session.execute(select(Message).where(Message.id == target_id))
    ).scalar_one()
    assert target_reloaded.is_active is False

    # version_count == 2 for that user turn (the original + the edited sibling).
    version_info = await derive_version_info(db_session, _uuid.UUID(conv_id))
    assert version_info[target_id][1] == 2
    assert version_info[new_user_id][1] == 2

    # active_leaf points at the new assistant node.
    conv = (
        await db_session.execute(
            select(Conversation).where(Conversation.id == _uuid.UUID(conv_id))
        )
    ).scalar_one()
    assert conv.active_leaf_id == new_assistant.id


async def test_edit_carries_over_attachments(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Target user message has ≥1 chat_image and ≥1 chat_file; after the edit the
    NEW user node has copies (same data/metadata, new id, new message_id); the OLD
    node's attachments are untouched."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Вижу вложения"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])
    # First turn carries an image AND a file attachment.
    await _send_first_turn(
        http_client,
        token,
        conv_id,
        [
            {"type": "text", "text": "Что во вложениях?"},
            {"type": "image_url", "image_url": {"url": _PNG_DATA_URI}},
            {
                "type": "file",
                "file": {"filename": "note.txt", "file_data": _TXT_DATA_URI},
            },
        ],
    )

    db_session.expire_all()
    old_user = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "user",
            )
        )
    ).scalar_one()
    old_user_id = old_user.id
    src_image = (
        await db_session.execute(
            select(ChatImage).where(ChatImage.message_id == old_user_id)
        )
    ).scalar_one()
    src_image_id = src_image.id
    src_image_b64 = src_image.full_base64
    src_image_media = src_image.media_type
    src_file = (
        await db_session.execute(
            select(ChatFile).where(ChatFile.message_id == old_user_id)
        )
    ).scalar_one()
    src_file_id = src_file.id
    src_file_b64 = src_file.base64_data
    src_file_name = src_file.filename
    src_file_kind = src_file.kind

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Уточняю"),
        StreamChunk(type="usage", input_tokens=8, output_tokens=4),
        StreamChunk(type="done"),
    ])
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{old_user_id}/edit",
        json={"content": "Опиши вложения подробнее"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    done = next(e for e in _parse_sse(resp.text) if e["event"] == "message_done")

    db_session.expire_all()
    new_assistant = (
        await db_session.execute(
            select(Message).where(Message.id == _uuid.UUID(done["data"]["message_id"]))
        )
    ).scalar_one()
    new_user_id = new_assistant.parent_id

    # The image row was COPIED to the new node (new id, identical payload/meta).
    copied_img = (
        await db_session.execute(
            select(ChatImage).where(ChatImage.message_id == new_user_id)
        )
    ).scalar_one()
    assert copied_img.id != src_image_id
    assert copied_img.full_base64 == src_image_b64
    assert copied_img.media_type == src_image_media

    # The file row was COPIED too (new id, identical payload/meta).
    copied_file = (
        await db_session.execute(
            select(ChatFile).where(ChatFile.message_id == new_user_id)
        )
    ).scalar_one()
    assert copied_file.id != src_file_id
    assert copied_file.base64_data == src_file_b64
    assert copied_file.filename == src_file_name
    assert copied_file.kind == src_file_kind

    # Originals still on the original node, untouched.
    img_still_there = (
        await db_session.execute(
            select(func.count()).select_from(ChatImage).where(
                ChatImage.message_id == old_user_id
            )
        )
    ).scalar_one()
    file_still_there = (
        await db_session.execute(
            select(func.count()).select_from(ChatFile).where(
                ChatFile.message_id == old_user_id
            )
        )
    ).scalar_one()
    assert img_still_there == 1
    assert file_still_there == 1


async def test_edit_context_uses_edited_text(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§0.3: the messages handed to the proxy END at the edited text — the new
    content is present and the OLD user content + old tail are ABSENT. Confirms
    append_user_turn=False + active-history correctness (no duplicated turn)."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)

    _LAST_CONTEXTS.clear()
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="ОтветА1"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])
    await _send_first_turn(http_client, token, conv_id, "ОригиналU1")

    db_session.expire_all()
    target = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "user",
            )
        )
    ).scalar_one()
    target_id = target.id

    _LAST_CONTEXTS.clear()
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="ОтветА1-edit"),
        StreamChunk(type="usage", input_tokens=8, output_tokens=4),
        StreamChunk(type="done"),
    ])
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{target_id}/edit",
        json={"content": "ИзменённыйU1"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text

    # Exactly one proxy call; its context ends at the EDITED user turn, with the
    # original prompt + the deactivated assistant tail absent (§0.3, single
    # trailing user turn — NOT an appended duplicate).
    assert len(_LAST_CONTEXTS) == 1
    ctx = _LAST_CONTEXTS[0]
    rendered = json.dumps(ctx, ensure_ascii=False)
    assert "ОригиналU1" not in rendered  # old user content absent
    assert "ОтветА1" not in rendered  # deactivated assistant tail absent
    assert ctx[-1]["role"] == "user"  # §0.3: ends at the user turn, no empty append
    assert "ИзменённыйU1" in json.dumps(ctx[-1]["content"], ensure_ascii=False)
    # Exactly one user turn in context (no duplicated append).
    user_turns = [m for m in ctx if m["role"] == "user"]
    assert len(user_turns) == 1


async def test_edit_inherits_thinking_from_active_reply(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edit must inherit the THINKING posture of the reply it replaces — exactly
    like regenerate (which inherits from target.reasoning_tokens). If the active
    assistant answer used reasoning (reasoning_tokens > 0), the edited branch's
    forced regeneration is generated WITH thinking too.

    Regression: edit_message forwarded no thinking flag, so process_chat_request
    defaulted thinking_enabled=False and the proxy got reasoning_budget=None —
    silently downgrading the edited answer to no-thinking, even in a thinking chat
    where the edited prompt explicitly asks the model to think.
    """
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    # Conversation on a reasoning-capable model so the thinking gate can fire.
    resp = await http_client.post(
        CONV_URL,
        json={"model_id": REASONING_MODEL_ID, "title": "Тест"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    conv_id = resp.json()["id"]

    # First turn: a thinking answer (the usage chunk reports reasoning tokens, so
    # the saved active reply has reasoning_tokens > 0).
    _LAST_REASONING_BUDGETS.clear()
    _install_proxy(monkeypatch, [
        StreamChunk(type="reasoning", delta="думаю"),
        StreamChunk(type="content", delta="Первый ответ"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5, reasoning_tokens=42),
        StreamChunk(type="done"),
    ])
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages",
        json={
            "content": "Вопрос",
            "model": REASONING_MODEL_ID,
            "thinking_enabled": True,
            "thinking_effort": "medium",
        },
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert _parse_sse(resp.text)[-1]["event"] == "message_done"

    db_session.expire_all()
    user_msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "user",
            )
        )
    ).scalar_one()
    user_msg_id = user_msg.id

    # Edit the user message. The reply being replaced used reasoning → the edit's
    # single generation must request reasoning too.
    _LAST_REASONING_BUDGETS.clear()
    _install_proxy(monkeypatch, [
        StreamChunk(type="reasoning", delta="думаю ещё"),
        StreamChunk(type="content", delta="Второй ответ"),
        StreamChunk(type="usage", input_tokens=8, output_tokens=4, reasoning_tokens=33),
        StreamChunk(type="done"),
    ])
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{user_msg_id}/edit",
        json={"content": "Изменённый вопрос, подумай"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert _parse_sse(resp.text)[-1]["event"] == "message_done"

    # Exactly one generation, and it requested a positive reasoning budget.
    assert len(_LAST_REASONING_BUDGETS) == 1, _LAST_REASONING_BUDGETS
    assert (
        _LAST_REASONING_BUDGETS[0] is not None and _LAST_REASONING_BUDGETS[0] > 0
    ), _LAST_REASONING_BUDGETS


async def test_edit_no_thinking_when_reply_had_none(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conversely, when the replaced reply did NOT use reasoning, the edited branch
    is generated WITHOUT thinking (reasoning_budget None) — inheritance, not
    always-on. Guards against over-correcting the regression."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    resp = await http_client.post(
        CONV_URL,
        json={"model_id": REASONING_MODEL_ID, "title": "Тест"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    conv_id = resp.json()["id"]

    # First turn: a plain answer, no reasoning tokens.
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Первый ответ"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages",
        json={"content": "Вопрос", "model": REASONING_MODEL_ID},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert _parse_sse(resp.text)[-1]["event"] == "message_done"

    db_session.expire_all()
    user_msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "user",
            )
        )
    ).scalar_one()
    user_msg_id = user_msg.id

    _LAST_REASONING_BUDGETS.clear()
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Второй ответ"),
        StreamChunk(type="usage", input_tokens=8, output_tokens=4),
        StreamChunk(type="done"),
    ])
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{user_msg_id}/edit",
        json={"content": "Изменённый вопрос"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert _parse_sse(resp.text)[-1]["event"] == "message_done"

    assert len(_LAST_REASONING_BUDGETS) == 1, _LAST_REASONING_BUDGETS
    assert _LAST_REASONING_BUDGETS[0] is None, _LAST_REASONING_BUDGETS


async def _set_web_search_support(
    db: AsyncSession, model_id: str, value: bool
) -> bool:
    """Pin the current pricing row's supports_web_search flag; return the old value
    so the test can restore it (gpt-4.1-mini has web search disabled by default)."""
    row = (
        await db.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == model_id,
                ModelPricing.is_current.is_(True),
            )
        )
    ).scalar_one()
    old = row.supports_web_search
    row.supports_web_search = value
    await db.commit()
    return old


async def test_edit_inherits_web_search_from_active_reply(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edit must inherit the WEB-SEARCH posture of the reply it replaces —
    exactly like regenerate (which recovers it from the target's usage_log via
    _was_web_search). If the active assistant answer used web search, the edited
    branch's forced regeneration runs WITH the web plugin too.

    Regression: edit_message forwarded no web_search flag, so process_chat_request
    defaulted web_search_enabled=False and the proxy got plugins=None — silently
    dropping web search on the edited answer in a web-search chat.
    """
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)

    old = await _set_web_search_support(db_session, MODEL_ID, True)
    try:
        # First turn: a web-search answer (the send body enables web search, so the
        # saved generation's usage_log records web_search_enabled=True).
        _LAST_PLUGINS.clear()
        _install_proxy(monkeypatch, [
            StreamChunk(type="content", delta="Первый ответ"),
            StreamChunk(type="usage", input_tokens=10, output_tokens=5),
            StreamChunk(type="done"),
        ])
        resp = await http_client.post(
            f"{CONV_URL}/{conv_id}/messages",
            json={
                "content": "Что нового?",
                "model": MODEL_ID,
                "web_search_enabled": True,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert _parse_sse(resp.text)[-1]["event"] == "message_done"

        db_session.expire_all()
        user_msg = (
            await db_session.execute(
                select(Message).where(
                    Message.conversation_id == _uuid.UUID(conv_id),
                    Message.role == "user",
                )
            )
        ).scalar_one()
        user_msg_id = user_msg.id

        # Edit the user message. The reply being replaced used web search → the
        # edit's single generation must carry the web plugin too.
        _LAST_PLUGINS.clear()
        _install_proxy(monkeypatch, [
            StreamChunk(type="content", delta="Второй ответ"),
            StreamChunk(type="usage", input_tokens=8, output_tokens=4),
            StreamChunk(type="done"),
        ])
        resp = await http_client.post(
            f"{CONV_URL}/{conv_id}/messages/{user_msg_id}/edit",
            json={"content": "Что нового сегодня?"},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert _parse_sse(resp.text)[-1]["event"] == "message_done"
    finally:
        await _set_web_search_support(db_session, MODEL_ID, old)

    # Exactly one generation, and it forwarded a non-empty plugins list containing
    # the web-search plugin.
    assert len(_LAST_PLUGINS) == 1, _LAST_PLUGINS
    assert _LAST_PLUGINS[0], _LAST_PLUGINS
    assert any(p.get("id") == "web" for p in _LAST_PLUGINS[0]), _LAST_PLUGINS


async def test_edit_no_web_search_when_reply_had_none(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conversely, when the replaced reply did NOT use web search, the edited branch
    is generated WITHOUT the web plugin (plugins None) — inheritance, not
    always-on. Guards against over-correcting the regression."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)

    # First turn: a plain answer, web search NOT requested.
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Первый ответ"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])
    await _send_first_turn(http_client, token, conv_id, "Привет")

    db_session.expire_all()
    user_msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "user",
            )
        )
    ).scalar_one()
    user_msg_id = user_msg.id

    _LAST_PLUGINS.clear()
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Второй ответ"),
        StreamChunk(type="usage", input_tokens=8, output_tokens=4),
        StreamChunk(type="done"),
    ])
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{user_msg_id}/edit",
        json={"content": "Привет, исправлено"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert _parse_sse(resp.text)[-1]["event"] == "message_done"

    assert len(_LAST_PLUGINS) == 1, _LAST_PLUGINS
    assert _LAST_PLUGINS[0] is None, _LAST_PLUGINS


async def test_edit_rejects_assistant_target(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-user target (assistant) is rejected with 400 (Russian error)."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])
    await _send_first_turn(http_client, token, conv_id, "Привет")

    db_session.expire_all()
    assistant_msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "assistant",
            )
        )
    ).scalar_one()
    assistant_id = assistant_msg.id

    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{assistant_id}/edit",
        json={"content": "нельзя"},
        headers=_auth(token),
    )
    assert resp.status_code == 400, resp.text


async def test_edit_insufficient_funds_reverts_to_prior_answer(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tr-yqmh: an edit whose reservation fails (insufficient funds) raises in the
    pre-stream STEP-4, BEFORE the streaming restore handler — and the edit already
    deactivated the prior answer and committed it. Without restoring, the turn is
    orphaned (the edited question with no answer). The failed edit must instead
    revert to the prior answered turn; the edited sibling is parked inactive.
    """
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Рим."),
        StreamChunk(type="usage", input_tokens=4, output_tokens=2),
        StreamChunk(type="done"),
    ])
    await _send_first_turn(http_client, token, conv_id, "Столица Италии?")

    db_session.expire_all()
    conv_uuid = _uuid.UUID(conv_id)
    user_msg = (await db_session.execute(
        select(Message).where(Message.conversation_id == conv_uuid, Message.role == "user")
    )).scalar_one()
    answer = (await db_session.execute(
        select(Message).where(Message.conversation_id == conv_uuid, Message.role == "assistant")
    )).scalar_one()
    # Drain the balance so the edit's reservation fails with insufficient funds.
    user = (
        await db_session.execute(select(User).where(User.id == _uuid.UUID(user_id)))
    ).scalar_one()
    user.balance_micro_rub = 0
    user.reserved_micro_rub = 0
    await db_session.commit()

    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{user_msg.id}/edit",
        json={"content": "Столица Греции?", "model": MODEL_ID},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    assert any(
        e.get("event") == "error" and e.get("data", {}).get("code") == "insufficient_funds"
        for e in events
    ), events

    # The prior answered turn is restored; the edited sibling is parked inactive.
    db_session.expire_all()
    rows = {
        (m.role, m.content): m.is_active
        for m in (
            await db_session.execute(
                select(Message).where(Message.conversation_id == conv_uuid)
            )
        ).scalars()
    }
    assert rows[("user", "Столица Италии?")] is True
    assert rows[("assistant", "Рим.")] is True
    assert rows[("user", "Столица Греции?")] is False
    conv = (
        await db_session.execute(select(Conversation).where(Conversation.id == conv_uuid))
    ).scalar_one()
    assert conv.active_leaf_id == answer.id
