"""Tests for POST /api/v1/chat/conversations/{id}/messages/{message_id}/regenerate (Task 6).

Billing-critical. Regenerate runs an INDEPENDENT process_chat_request: the new
assistant node is a SIBLING/version of the target (shares parent_id), billed once
with a fresh request_id, and the original is deactivated. Corrections applied:
- §0.1: STEP-7 / _wire_assistant_branch is the SINGLE source of truth for the new
  node's parent_id + active leaf — the endpoint does NOT re-parent post-stream.
- §0.3: append_user_turn=False — the active history already ends at the user turn
  to re-answer; NO trailing empty user turn is appended to context/estimation.
- §0.4: a mid-stream disconnect persists the partial node on the active branch.
- §0.5: an actual cost above balance is clamped (overdraft absorbed, never negative).
"""
import json
import uuid as _uuid

import pytest
import pytest_asyncio
from fastapi import Request
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.api.v1.chat as chat_module
import app.services.openrouter_proxy as openrouter_proxy_module
from app.models.audit_log import AdminAuditLog
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.notification import Notification
from app.models.reservation import Reservation
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services.billing_service import credit_balance
from app.services.branching_service import derive_version_info
from app.services.openrouter_proxy import StreamChunk

REGISTER_URL = "/api/v1/auth/register"
CONV_URL = "/api/v1/chat/conversations"
MODEL_ID = "openai/gpt-4.1-mini"

_TEST_EMAILS = ["regen_test_a@example.com", "regen_test_b@example.com"]


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
        # Null active_leaf_id before deleting messages so the FK doesn't block.
        await db_session.execute(
            sa_delete(Message).where(
                Message.conversation_id.in_(
                    select(Conversation.id).where(Conversation.user_id == uid)
                )
            )
        )
        await db_session.execute(sa_delete(Conversation).where(Conversation.user_id == uid))
        await db_session.execute(sa_delete(UsageLog).where(UsageLog.user_id == uid))
        await db_session.execute(sa_delete(Reservation).where(Reservation.user_id == uid))
        await db_session.execute(sa_delete(Transaction).where(Transaction.user_id == uid))
        await db_session.execute(sa_delete(Notification).where(Notification.user_id == uid))
        await db_session.execute(sa_delete(AdminAuditLog).where(AdminAuditLog.actor_id == uid))
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


async def _seed_balance(db: AsyncSession, user_id_str: str, amount_micro_rub: int) -> None:
    await credit_balance(
        db,
        user_id=_uuid.UUID(user_id_str),
        amount_micro_rub=amount_micro_rub,
        idempotency_key=f"regen-seed-{user_id_str}-{_uuid.uuid4()}",
        description="Regenerate test seed",
    )
    await db.commit()


async def _set_balance(db: AsyncSession, user_id_str: str, amount_micro_rub: int) -> None:
    """Force an exact balance (used for the overdraft-clamp seed)."""
    uid = _uuid.UUID(user_id_str)
    user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
    user.balance_micro_rub = amount_micro_rub
    user.reserved_micro_rub = 0
    await db.commit()


async def _create_conversation(http_client: AsyncClient, token: str) -> str:
    resp = await http_client.post(
        CONV_URL,
        json={"model_id": MODEL_ID, "title": "Тест"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


# Captures the context messages the proxy was handed on each call so the
# context-exclusion test can assert the deactivated tail/sibling never leak in.
_LAST_CONTEXTS: list[list[dict]] = []


def _install_proxy(monkeypatch: pytest.MonkeyPatch, chunks) -> None:
    class _FakeProxy:
        def __init__(self, db):
            self.db = db

        async def proxy_to_openrouter_with_retry(
            self, messages, model_id, max_retries=3, plugins=None, reasoning_budget=None
        ):
            _LAST_CONTEXTS.append([dict(m) for m in messages])
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


async def _send_turn(http_client: AsyncClient, token: str, conv_id: str, text: str) -> dict:
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages",
        json={"content": text, "model": MODEL_ID},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    return next(e for e in events if e["event"] == "message_done")["data"]


# Records the kwargs process_chat_request was invoked with, so a test can assert
# the endpoint forwarded a toggle (tools_enabled etc.) into the billing pipeline
# without exercising the full agentic loop.
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


async def _seed_target_assistant(
    http_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
    token: str, conv_id: str,
):
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ"),
        StreamChunk(type="usage", input_tokens=4, output_tokens=2),
        StreamChunk(type="done"),
    ])
    await _send_turn(http_client, token, conv_id, "Вопрос")
    db_session.expire_all()
    assistant = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "assistant",
            )
        )
    ).scalar_one()
    return assistant.id


async def test_regenerate_forwards_tools_enabled(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tr-1hm: the live «Инструменты» toggle must reach process_chat_request so a
    regenerated tool-answer actually runs the agentic loop (not a flat single-shot)."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)
    assistant_id = await _seed_target_assistant(
        http_client, db_session, monkeypatch, token, conv_id
    )

    captured: dict = {}
    _install_pcr_recorder(monkeypatch, captured)
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{assistant_id}/regenerate",
        json={"tools_enabled": True},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert captured.get("tools_enabled") is True


async def test_regenerate_uses_overridden_model(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tr-77u: a regenerate with an explicit `model` (the picker's CURRENT model,
    different from the conversation's stored model) must use that NEW model — both
    forwarded to process_chat_request AND persisted to conversation.model_id, so the
    regenerated answer is priced/recorded against the model the user just picked
    (parity with send), not the stale model from the prior send."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)
    assistant_id = await _seed_target_assistant(
        http_client, db_session, monkeypatch, token, conv_id
    )

    new_model = "anthropic/claude-sonnet-4-6"
    assert new_model != MODEL_ID

    captured: dict = {}
    _install_pcr_recorder(monkeypatch, captured)
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{assistant_id}/regenerate",
        json={"model": new_model},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    # The NEW model reaches the billing pipeline (it prices that model).
    assert captured.get("model_id") == new_model
    # And the conversation reflects it for subsequent reads (mirrors send_message).
    db_session.expire_all()
    conv = (
        await db_session.execute(
            select(Conversation).where(Conversation.id == _uuid.UUID(conv_id))
        )
    ).scalar_one()
    assert conv.model_id == new_model


async def test_regenerate_defaults_tools_enabled_false(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitted ⇒ tools off (conservative: no surprise multi-round spend)."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)
    assistant_id = await _seed_target_assistant(
        http_client, db_session, monkeypatch, token, conv_id
    )

    captured: dict = {}
    _install_pcr_recorder(monkeypatch, captured)
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{assistant_id}/regenerate",
        json={},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert captured.get("tools_enabled") is False


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_regenerate_billing_independence(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regenerate runs an independent process_chat_request: a NEW reservation,
    a NEW debit transaction, and a NEW usage_log with a request_id distinct from
    the original generation. Balance is debited exactly once for the regen, and
    the new node is an active SIBLING (version) of the target."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)

    _LAST_CONTEXTS.clear()
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Первый ответ"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])
    first_done = await _send_turn(http_client, token, conv_id, "Вопрос")
    original_request_id = first_done["request_id"]

    db_session.expire_all()
    uid = _uuid.UUID(user_id)
    assistant = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "assistant",
            )
        )
    ).scalar_one()
    # Capture the scalar id now — a later expire_all() would make `assistant.id`
    # a sync lazy-load (MissingGreenlet) under asyncpg.
    assistant_id = assistant.id
    balance_before = (
        await db_session.execute(select(User.balance_micro_rub).where(User.id == uid))
    ).scalar_one()

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Второй ответ"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=7),
        StreamChunk(type="done"),
    ])
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{assistant_id}/regenerate",
        json={},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    names = [e["event"] for e in events]
    assert names[0] == "message_start"
    assert names[-1] == "message_done"
    regen_done = next(e for e in events if e["event"] == "message_done")["data"]
    assert "message_id" in regen_done
    assert "active_leaf_id" in regen_done

    # Distinct request_id ⇒ distinct reservation/debit/usage_log.
    assert regen_done["request_id"] != original_request_id

    db_session.expire_all()
    # Exactly TWO usage_logs (one per generation), both with distinct request_ids;
    # the UNIQUE(request_id) constraint is never violated.
    usage_rows = (
        await db_session.execute(select(UsageLog).where(UsageLog.user_id == uid))
    ).scalars().all()
    assert len(usage_rows) == 2
    assert len({str(u.request_id) for u in usage_rows}) == 2
    # Exactly TWO reservations (one per generation).
    reservations = (
        await db_session.execute(select(Reservation).where(Reservation.user_id == uid))
    ).scalars().all()
    assert len(reservations) == 2
    # Regen produced exactly one additional debit transaction.
    debits = (
        await db_session.execute(
            select(Transaction).where(
                Transaction.user_id == uid, Transaction.type == "debit"
            )
        )
    ).scalars().all()
    assert len(debits) == 2
    # Balance dropped exactly once (by the regen's actual cost), not twice/not zero.
    balance_after = (
        await db_session.execute(select(User.balance_micro_rub).where(User.id == uid))
    ).scalar_one()
    regen_cost = next(
        u.actual_cost_micro_rub
        for u in usage_rows
        if str(u.request_id) == regen_done["request_id"]
    )
    assert balance_before - balance_after == regen_cost
    assert regen_cost > 0

    # New assistant node is a SIBLING of the original (shares parent_id), active;
    # the original is deactivated. active_leaf points at the new node.
    new_assistant = (
        await db_session.execute(
            select(Message).where(Message.id == _uuid.UUID(regen_done["message_id"]))
        )
    ).scalar_one()
    original = (
        await db_session.execute(select(Message).where(Message.id == assistant_id))
    ).scalar_one()
    assert new_assistant.parent_id == original.parent_id
    assert new_assistant.is_active is True
    assert original.is_active is False
    conv = (
        await db_session.execute(
            select(Conversation).where(Conversation.id == _uuid.UUID(conv_id))
        )
    ).scalar_one()
    assert conv.active_leaf_id == new_assistant.id

    # version_count == 2 for that parent turn (the two assistant siblings).
    version_info = await derive_version_info(db_session, _uuid.UUID(conv_id))
    assert version_info[new_assistant.id][1] == 2
    assert version_info[original.id][1] == 2


async def test_regenerate_context_excludes_deactivated_tail_and_sibling(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§0.3: the messages handed to the proxy on regenerate are the active branch
    up to the parent user message ONLY. The context ENDS at the original user
    message (with the REAL prompt text present, NOT an empty string), and the
    deactivated target assistant + any tail are ABSENT (spec §7 case 2)."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)

    _LAST_CONTEXTS.clear()
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="ОтветА1"),
        StreamChunk(type="usage", input_tokens=4, output_tokens=2),
        StreamChunk(type="done"),
    ])
    # Turn 1: user "ВопросU1" -> assistant "ОтветА1" (this assistant is the target).
    await _send_turn(http_client, token, conv_id, "ВопросU1")

    db_session.expire_all()
    a1 = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "assistant",
            )
        )
    ).scalar_one()

    _LAST_CONTEXTS.clear()
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="ОтветА1-regen"),
        StreamChunk(type="usage", input_tokens=4, output_tokens=3),
        StreamChunk(type="done"),
    ])
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{a1.id}/regenerate",
        json={},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text

    # The context handed to the proxy for the regen excludes the deactivated A1
    # (replaced sibling). It ends at the parent user message "ВопросU1" — with the
    # real prompt text present, NOT an empty trailing user turn.
    assert len(_LAST_CONTEXTS) == 1
    ctx = _LAST_CONTEXTS[0]
    rendered = json.dumps(ctx, ensure_ascii=False)
    assert "ОтветА1" not in rendered  # deactivated assistant sibling absent
    assert ctx[-1]["role"] == "user"  # §0.3: ends at the user turn, not an empty append
    assert "ВопросU1" in json.dumps(ctx[-1]["content"], ensure_ascii=False)


async def test_regenerate_overdraft_clamp(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§0.5: when the regen's ACTUAL cost exceeds the balance, the overdraft is
    absorbed — the usage_log records overdraft_absorbed_micro_rub > 0 and the
    balance clamps to 0 (never negative)."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    # Enough balance to pass the (small) reservation estimate for both turns.
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)
    uid = _uuid.UUID(user_id)

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Первый ответ"),
        StreamChunk(type="usage", input_tokens=5, output_tokens=3),
        StreamChunk(type="done"),
    ])
    await _send_turn(http_client, token, conv_id, "Вопрос")

    db_session.expire_all()
    assistant = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "assistant",
            )
        )
    ).scalar_one()
    assistant_id = assistant.id

    # Drop the balance well below the regen's actual cost so the clamp fires.
    await _set_balance(db_session, user_id, 200_000)  # 0.20 RUB

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="a" * 200),
        StreamChunk(type="usage", input_tokens=5_000, output_tokens=20_000),  # ≫ balance
        StreamChunk(type="done"),
    ])
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{assistant_id}/regenerate",
        json={},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    regen_done = next(e for e in events if e["event"] == "message_done")["data"]

    db_session.expire_all()
    user = (await db_session.execute(select(User).where(User.id == uid))).scalar_one()
    assert user.balance_micro_rub == 0  # clamped, never negative

    regen_usage = (
        await db_session.execute(
            select(UsageLog).where(
                UsageLog.request_id == _uuid.UUID(regen_done["request_id"])
            )
        )
    ).scalar_one()
    assert regen_usage.status == "completed"
    assert regen_usage.overdraft_absorbed_micro_rub > 0


async def test_regenerate_partial_disconnect_persists_on_active_branch(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§0.4: a mid-stream disconnect on regenerate persists the partial node on
    the ACTIVE branch — parent_id = target.parent_id, is_active=true, the
    conversation's active_leaf points at it, cost is clamped, and there is exactly
    one usage_log per message."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)
    uid = _uuid.UUID(user_id)

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Первый ответ"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])
    await _send_turn(http_client, token, conv_id, "Вопрос")

    db_session.expire_all()
    target = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "assistant",
            )
        )
    ).scalar_one()
    target_id = target.id
    target_parent_id = target.parent_id

    # Force a mid-stream disconnect: Request.is_disconnected returns True on the
    # 2nd inter-chunk check so finalize_partial bills for the delivered content.
    calls = {"n": 0}

    async def _disc(self) -> bool:
        calls["n"] += 1
        return calls["n"] >= 2

    monkeypatch.setattr(Request, "is_disconnected", _disc)

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="частичный "),
        StreamChunk(type="content", delta="ответ"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{target_id}/regenerate",
        json={},
        headers=_auth(token),
    )
    # The client "disconnected" — the response may carry no message_done, but the
    # DB must reflect the persisted partial node on the active branch.
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    # The new assistant sibling = the active assistant that is NOT the (now
    # deactivated) target.
    partial = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "assistant",
                Message.is_active.is_(True),
            )
        )
    ).scalar_one()
    assert partial.id != target_id
    assert partial.parent_id == target_parent_id  # sibling of the target
    assert partial.is_active is True
    assert "частичный" in partial.content

    conv = (
        await db_session.execute(
            select(Conversation).where(Conversation.id == _uuid.UUID(conv_id))
        )
    ).scalar_one()
    assert conv.active_leaf_id == partial.id

    # Exactly one usage_log per message (the original send + the partial regen).
    usage_rows = (
        await db_session.execute(select(UsageLog).where(UsageLog.user_id == uid))
    ).scalars().all()
    assert len(usage_rows) == 2
    assert len({str(u.request_id) for u in usage_rows}) == 2
    # Balance never went negative (partial cost clamped where needed).
    user = (await db_session.execute(select(User).where(User.id == uid))).scalar_one()
    assert user.balance_micro_rub >= 0


async def test_regenerate_rejects_user_message(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regenerate targets an assistant message; pointing it at a user message 404s."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="resp"),
        StreamChunk(type="usage", input_tokens=2, output_tokens=1),
        StreamChunk(type="done"),
    ])
    await _send_turn(http_client, token, conv_id, "U1")

    db_session.expire_all()
    user_msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "user",
            )
        )
    ).scalar_one()
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{user_msg.id}/regenerate",
        json={},
        headers=_auth(token),
    )
    assert resp.status_code == 404, resp.text


async def test_regenerate_wrong_owner_returns_404(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_a, user_a = await _register(http_client, _TEST_EMAILS[0])
    token_b, _ = await _register(http_client, _TEST_EMAILS[1])
    await _seed_balance(db_session, user_a, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token_a)

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="resp"),
        StreamChunk(type="usage", input_tokens=2, output_tokens=1),
        StreamChunk(type="done"),
    ])
    await _send_turn(http_client, token_a, conv_id, "U1")

    db_session.expire_all()
    a1 = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id),
                Message.role == "assistant",
            )
        )
    ).scalar_one()
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{a1.id}/regenerate",
        json={},
        headers=_auth(token_b),
    )
    assert resp.status_code == 404, resp.text


async def test_wire_assistant_uses_explicit_parent(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regenerated assistant is wired off the EXPLICIT parent the endpoint
    passes (target.parent_id = the user turn the target answered), NOT the
    "last active node" the old wiring derived from the active path.

    Build a two-turn chat (U1─A1, U2─A2) so the active leaf at regen time (the
    parent user turn A1 answered, = U1) is NOT the conversation's last node. The
    new assistant must still parent off U1 — proving the explicit-parent wiring,
    not a derivation that would have picked U2/A2.
    """
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ1"),
        StreamChunk(type="usage", input_tokens=4, output_tokens=2),
        StreamChunk(type="done"),
    ])
    t1 = await _send_turn(http_client, token, conv_id, "Вопрос1")
    a1_id = t1["message_id"]

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ2"),
        StreamChunk(type="usage", input_tokens=4, output_tokens=2),
        StreamChunk(type="done"),
    ])
    await _send_turn(http_client, token, conv_id, "Вопрос2")

    # Capture A1's parent (U1) before regenerating.
    db_session.expire_all()
    a1 = (
        await db_session.execute(select(Message).where(Message.id == _uuid.UUID(a1_id)))
    ).scalar_one()
    a1_parent_id = a1.parent_id

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ1b"),
        StreamChunk(type="usage", input_tokens=4, output_tokens=2),
        StreamChunk(type="done"),
    ])
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{a1_id}/regenerate",
        json={},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    regen_done = next(
        e for e in _parse_sse(resp.text) if e["event"] == "message_done"
    )["data"]

    db_session.expire_all()
    new_assistant = (
        await db_session.execute(
            select(Message).where(Message.id == _uuid.UUID(regen_done["message_id"]))
        )
    ).scalar_one()
    # Sibling of A1: same explicit parent (U1), not the conversation's last node.
    assert new_assistant.parent_id == a1_parent_id
