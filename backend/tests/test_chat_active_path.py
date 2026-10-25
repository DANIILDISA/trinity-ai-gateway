"""PUT /api/v1/chat/conversations/{id}/active-path — free version switch (Task 8).

The switch is the FREE counterpart to regenerate/edit: it only rewrites
`messages.is_active` + `conversations.active_leaf_id` to re-render a different
sibling version of a turn. It NEVER calls the billing pipeline, so it writes no
transaction / reservation / usage_log row and leaves the balance byte-for-byte
unchanged (BILLING_LOGIC.md, spec §7 case 4).

These tests build genuine branched conversations through the real send +
regenerate endpoints (so the sibling versions, parent_id links, and active-leaf
bookkeeping match production), then drive the switch and assert the active path
flips, the §8 single-active-leaf invariant holds, and the switch is free.

Test hygiene: ORM scalar ids are captured into plain vars BEFORE any
`db_session.expire_all()` so a later attribute access can't trigger a sync lazy
load (MissingGreenlet) under asyncpg.
"""
import json
import uuid as _uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.services.openrouter_proxy import StreamChunk

REGISTER_URL = "/api/v1/auth/register"
CONV_URL = "/api/v1/chat/conversations"
MODEL_ID = "openai/gpt-4.1-mini"

_TEST_EMAILS = ["active_path_a@example.com", "active_path_b@example.com"]


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
        # Null the message tree first so active_leaf_id (FK -> messages) can't block.
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
        idempotency_key=f"switch-seed-{user_id_str}-{_uuid.uuid4()}",
        description="Active-path switch test seed",
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


def _install_proxy(monkeypatch: pytest.MonkeyPatch, chunks) -> None:
    class _FakeProxy:
        def __init__(self, db):
            self.db = db

        async def proxy_to_openrouter_with_retry(
            self, messages, model_id, max_retries=3, plugins=None, reasoning_budget=None
        ):
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


async def _send_turn(
    http_client: AsyncClient,
    token: str,
    conv_id: str,
    text: str,
    web_search_enabled: bool = False,
) -> dict:
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages",
        json={
            "content": text,
            "model": MODEL_ID,
            "web_search_enabled": web_search_enabled,
        },
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    return next(e for e in events if e["event"] == "message_done")["data"]


async def _get_conversation(
    http_client: AsyncClient, token: str, conv_id: str
) -> dict:
    resp = await http_client.get(f"{CONV_URL}/{conv_id}", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _regenerate(
    http_client: AsyncClient, token: str, conv_id: str, assistant_id: str
) -> dict:
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{assistant_id}/regenerate",
        json={},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    return next(e for e in events if e["event"] == "message_done")["data"]


async def _edit(
    http_client: AsyncClient, token: str, conv_id: str, message_id: str, content: str
) -> dict:
    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages/{message_id}/edit",
        json={"content": content},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    return next(e for e in events if e["event"] == "message_done")["data"]


async def _switch(
    http_client: AsyncClient, token: str, conv_id: str, node_id: str
) -> "tuple[int, list[dict] | dict]":
    resp = await http_client.put(
        f"{CONV_URL}/{conv_id}/active-path",
        json={"node_id": node_id},
        headers=_auth(token),
    )
    return resp.status_code, (resp.json() if resp.content else None)


async def _assert_single_active_leaf(
    db: AsyncSession, conv_id: _uuid.UUID
) -> _uuid.UUID:
    """The §8 invariant: exactly one active, non-deleted leaf, equal to
    active_leaf_id, AND the is_active walk from the root ends at it."""
    rows = (
        await db.execute(
            select(Message)
            .where(
                Message.conversation_id == conv_id,
                Message.is_active.is_(True),
                Message.deleted_at.is_(None),
            )
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    ).scalars().all()
    leaf = (
        await db.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()

    active_ids = {m.id for m in rows}
    parent_ids = {m.parent_id for m in rows if m.parent_id is not None}
    leaves = active_ids - parent_ids
    assert leaves == {leaf}, f"expected single active leaf {leaf}, got {leaves}"

    # Walk DOWN from the root following is_active children — it must end at leaf.
    by_parent: dict[_uuid.UUID | None, Message] = {}
    for m in rows:
        # Exactly one active child per parent on a single active path.
        assert m.parent_id not in by_parent, "two active siblings share a parent"
        by_parent[m.parent_id] = m
    node = by_parent.get(None)
    assert node is not None, "no active root"
    while node.id in {m.parent_id for m in rows if m.parent_id is not None}:
        node = by_parent[node.id]
    assert node.id == leaf, f"walk ended at {node.id}, expected leaf {leaf}"
    return leaf


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_switch_activates_chosen_version_and_path(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regenerate produces two assistant siblings (A then B, B active). Switching
    back to the now-inactive A re-renders A: A becomes active, B inactive,
    active_leaf_id repoints to A's branch leaf, and the returned path is [user, A]."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ A"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])
    first_done = await _send_turn(http_client, token, conv_id, "Вопрос")
    asst_a_id = _uuid.UUID(first_done["message_id"])

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ B"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=7),
        StreamChunk(type="done"),
    ])
    regen_done = await _regenerate(http_client, token, conv_id, str(asst_a_id))
    asst_b_id = _uuid.UUID(regen_done["message_id"])

    # Capture the shared user-turn id + confirm pre-switch state (B active).
    conv_uuid = _uuid.UUID(conv_id)
    db_session.expire_all()
    user_msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conv_uuid, Message.role == "user"
            )
        )
    ).scalar_one()
    user_msg_id = user_msg.id
    a_pre = (
        await db_session.execute(select(Message).where(Message.id == asst_a_id))
    ).scalar_one()
    b_pre = (
        await db_session.execute(select(Message).where(Message.id == asst_b_id))
    ).scalar_one()
    assert a_pre.is_active is False  # regenerate deactivated A
    assert b_pre.is_active is True

    # Switch back to the inactive sibling A.
    status, path = await _switch(http_client, token, conv_id, str(asst_a_id))
    assert status == 200, path

    # Returned active path is [user, A].
    assert [m["id"] for m in path] == [str(user_msg_id), str(asst_a_id)]
    assert path[-1]["content"] == "Ответ A"

    # DB re-derivation: A active, B deactivated, active_leaf_id repointed to A.
    db_session.expire_all()
    a_row = (
        await db_session.execute(select(Message).where(Message.id == asst_a_id))
    ).scalar_one()
    b_row = (
        await db_session.execute(select(Message).where(Message.id == asst_b_id))
    ).scalar_one()
    conv_row = (
        await db_session.execute(select(Conversation).where(Conversation.id == conv_uuid))
    ).scalar_one()
    assert a_row.is_active is True
    assert b_row.is_active is False
    assert conv_row.active_leaf_id == asst_a_id


async def test_switch_response_carries_version_counts(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch response must carry each node's version_index/version_count so
    the < n/m > switcher stays visible after switching.

    Regression: switch_active_path serialized the path WITHOUT version_info, so
    every node defaulted to (1, 1). The frontend hides the switcher when
    version_count == 1, collapsing it after the user switched to an earlier
    sibling — stranding them on that version with no way back to the others.
    """
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ A"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])
    first_done = await _send_turn(http_client, token, conv_id, "Вопрос")
    asst_a_id = first_done["message_id"]

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ B"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=7),
        StreamChunk(type="done"),
    ])
    await _regenerate(http_client, token, conv_id, asst_a_id)

    # Switch back to sibling A (the earlier-created of two assistant versions).
    status, path = await _switch(http_client, token, conv_id, asst_a_id)
    assert status == 200, path

    # The assistant node in the returned active path must advertise BOTH versions
    # so the frontend keeps rendering the < 1/2 > switcher.
    asst = next(m for m in path if m["role"] == "assistant")
    assert asst["version_count"] == 2, path
    assert asst["version_index"] == 1  # A is the earlier-created sibling


async def test_get_conversation_restores_web_search_flag(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh GET must report web_search=true for an assistant turn generated
    with web-search on (recovered from its usage_log), and false for a turn sent
    without it. Otherwise the meta-row globe disappears after a reload (tr-8o7).

    The flag is keyed by request_id off usage_logs — no per-message column — so
    this also guards the join not regressing branch_totals / version_info."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)

    # Turn 1: web-search ON.
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="С поиском"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])
    search_done = await _send_turn(
        http_client, token, conv_id, "Вопрос с поиском", web_search_enabled=True
    )
    search_asst_id = search_done["message_id"]

    # Turn 2: web-search OFF.
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Без поиска"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])
    plain_done = await _send_turn(
        http_client, token, conv_id, "Вопрос без поиска", web_search_enabled=False
    )
    plain_asst_id = plain_done["message_id"]

    # Fresh GET — the live SSE state is gone; web_search must come from usage_logs.
    body = await _get_conversation(http_client, token, conv_id)
    by_id = {m["id"]: m for m in body["messages"]}

    assert by_id[search_asst_id]["web_search"] is True
    assert by_id[plain_asst_id]["web_search"] is False
    # User turns never carry the flag (no request_id / usage_log).
    for m in body["messages"]:
        if m["role"] == "user":
            assert m["web_search"] is False


async def test_get_conversation_restores_stopped_flag(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh GET must report stopped=true for an assistant turn whose usage_log
    was finalized as 'partial' (a Stop mid-reply), and false for a normally
    'completed' turn — so a reloaded stopped reply is distinguishable from a
    completed one (tr-fo1). The flag is derived on the read path from
    usage_logs.status keyed by request_id, with no per-message column.

    GUARD: only 'partial' ⇒ stopped. A 'failed' or 'refunded' usage_log must NOT
    be mis-read as stopped (those are terminal money states, not a user Stop)."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)

    # Four assistant turns, each finalized 'completed' by the normal send path; we
    # then rewrite each one's usage_log.status to exercise every branch.
    statuses = ["partial", "completed", "failed", "refunded"]
    asst_by_status: dict[str, str] = {}
    for status in statuses:
        _install_proxy(monkeypatch, [
            StreamChunk(type="content", delta=f"ответ {status}"),
            StreamChunk(type="usage", input_tokens=10, output_tokens=5),
            StreamChunk(type="done"),
        ])
        done = await _send_turn(http_client, token, conv_id, f"Вопрос {status}")
        asst_by_status[status] = done["message_id"]
        # Rewrite the just-written usage_log to the status under test.
        await db_session.execute(
            UsageLog.__table__.update()
            .where(UsageLog.request_id == _uuid.UUID(done["request_id"]))
            .values(status=status)
        )
    await db_session.commit()

    # Fresh GET — the live SSE state is gone; stopped must come from usage_logs.
    body = await _get_conversation(http_client, token, conv_id)
    by_id = {m["id"]: m for m in body["messages"]}

    # Only 'partial' surfaces as stopped.
    assert by_id[asst_by_status["partial"]]["stopped"] is True
    # Non-partial statuses must NOT be mis-read as stopped.
    assert by_id[asst_by_status["completed"]]["stopped"] is False
    assert by_id[asst_by_status["failed"]]["stopped"] is False
    assert by_id[asst_by_status["refunded"]]["stopped"] is False
    # User turns never carry the flag (no request_id / usage_log).
    for m in body["messages"]:
        if m["role"] == "user":
            assert m["stopped"] is False


async def test_switch_is_free_no_balance_touch(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch writes NO transaction / reservation / usage_log row and leaves
    balance + reserved byte-for-byte unchanged (spec §7 case 4)."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)
    uid = _uuid.UUID(user_id)

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ A"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])
    first_done = await _send_turn(http_client, token, conv_id, "Вопрос")
    asst_a_id = first_done["message_id"]

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ B"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=7),
        StreamChunk(type="done"),
    ])
    await _regenerate(http_client, token, conv_id, asst_a_id)

    # Snapshot the full financial state BEFORE the switch.
    db_session.expire_all()
    user_before = (
        await db_session.execute(select(User).where(User.id == uid))
    ).scalar_one()
    balance_before = user_before.balance_micro_rub
    reserved_before = user_before.reserved_micro_rub
    txn_before = (
        await db_session.execute(
            select(func.count()).select_from(Transaction).where(
                Transaction.user_id == uid
            )
        )
    ).scalar()
    res_before = (
        await db_session.execute(
            select(func.count()).select_from(Reservation).where(
                Reservation.user_id == uid
            )
        )
    ).scalar()
    usage_before = (
        await db_session.execute(
            select(func.count()).select_from(UsageLog).where(UsageLog.user_id == uid)
        )
    ).scalar()

    status, _path = await _switch(http_client, token, conv_id, asst_a_id)
    assert status == 200

    # FREE: nothing financial moved.
    db_session.expire_all()
    user_after = (
        await db_session.execute(select(User).where(User.id == uid))
    ).scalar_one()
    assert user_after.balance_micro_rub == balance_before
    assert user_after.reserved_micro_rub == reserved_before
    txn_after = (
        await db_session.execute(
            select(func.count()).select_from(Transaction).where(
                Transaction.user_id == uid
            )
        )
    ).scalar()
    res_after = (
        await db_session.execute(
            select(func.count()).select_from(Reservation).where(
                Reservation.user_id == uid
            )
        )
    ).scalar()
    usage_after = (
        await db_session.execute(
            select(func.count()).select_from(UsageLog).where(UsageLog.user_id == uid)
        )
    ).scalar()
    assert txn_after == txn_before
    assert res_after == res_before
    assert usage_after == usage_before


async def test_switch_invariant_single_active_leaf(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a switch, exactly one active leaf survives and the is_active walk
    from the root ends at active_leaf_id (§8 invariant)."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)
    conv_uuid = _uuid.UUID(conv_id)

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ A"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])
    first_done = await _send_turn(http_client, token, conv_id, "Вопрос")
    asst_a_id = first_done["message_id"]

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ B"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=7),
        StreamChunk(type="done"),
    ])
    await _regenerate(http_client, token, conv_id, asst_a_id)

    status, _path = await _switch(http_client, token, conv_id, asst_a_id)
    assert status == 200

    db_session.expire_all()
    leaf = await _assert_single_active_leaf(db_session, conv_uuid)
    assert leaf == _uuid.UUID(asst_a_id)


async def test_switch_rejects_foreign_or_unknown_node(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nonexistent node, or one from another user's conversation, is rejected
    (404), and an unrelated rejection leaves the active path intact."""
    token_a, user_a = await _register(http_client, _TEST_EMAILS[0])
    token_b, user_b = await _register(http_client, _TEST_EMAILS[1])
    await _seed_balance(db_session, user_a, 1_000_000_000)
    await _seed_balance(db_session, user_b, 1_000_000_000)
    conv_a = await _create_conversation(http_client, token_a)
    conv_b = await _create_conversation(http_client, token_b)

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ A"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])
    a_done = await _send_turn(http_client, token_a, conv_a, "ВопросA")
    a_assistant_id = a_done["message_id"]

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ B"),
        StreamChunk(type="usage", input_tokens=10, output_tokens=5),
        StreamChunk(type="done"),
    ])
    b_done = await _send_turn(http_client, token_b, conv_b, "ВопросB")
    b_assistant_id = b_done["message_id"]

    conv_a_uuid = _uuid.UUID(conv_a)

    # 1) Unknown node id → 404.
    status, _ = await _switch(http_client, token_a, conv_a, str(_uuid.uuid4()))
    assert status == 404

    # 2) A node that belongs to conv_b, addressed via conv_a → 404 (not this conv).
    status, _ = await _switch(http_client, token_a, conv_a, b_assistant_id)
    assert status == 404

    # 3) A different user trying to switch conv_a → 404 (ownership).
    status, _ = await _switch(http_client, token_b, conv_a, a_assistant_id)
    assert status == 404

    # The rejected switches left conv_a's active path untouched and consistent.
    db_session.expire_all()
    leaf = await _assert_single_active_leaf(db_session, conv_a_uuid)
    assert leaf == _uuid.UUID(a_assistant_id)


async def test_switch_past_turn_sets_aside_later_turns(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-turn 'branch & set aside' (spec §2): regenerate the FIRST turn after
    a second turn already exists, then switch the first turn back to its original
    version. The later turns of the previously-active branch are deactivated (set
    aside, not deleted) and the chosen branch is rendered.

    Tree built:
        U1 ─ A1            (turn 1, original)
             └ U2 ─ A2     (turn 2, chained off A1)
        U1 ─ A1b           (turn 1 regenerated → new active branch, A1+U2+A2 set aside)

    Switching back to A1 must re-activate A1 and its set-aside continuation
    (U2 ─ A2) while deactivating A1b; nothing is deleted.
    """
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)
    conv_uuid = _uuid.UUID(conv_id)

    # Turn 1.
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ1"),
        StreamChunk(type="usage", input_tokens=4, output_tokens=2),
        StreamChunk(type="done"),
    ])
    t1 = await _send_turn(http_client, token, conv_id, "Вопрос1")
    a1_id = t1["message_id"]

    # Turn 2 (chains off A1).
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ2"),
        StreamChunk(type="usage", input_tokens=4, output_tokens=2),
        StreamChunk(type="done"),
    ])
    t2 = await _send_turn(http_client, token, conv_id, "Вопрос2")
    a2_id = t2["message_id"]

    # Capture the second user turn (U2) id before further mutation.
    db_session.expire_all()
    u2 = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conv_uuid,
                Message.role == "user",
                Message.content == "Вопрос2",
            )
        )
    ).scalar_one()
    u2_id = u2.id

    # Regenerate turn 1 → A1b. This deactivates A1 and its whole tail (U2, A2)
    # and renders the new single-turn branch [U1, A1b].
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ1b"),
        StreamChunk(type="usage", input_tokens=4, output_tokens=2),
        StreamChunk(type="done"),
    ])
    regen = await _regenerate(http_client, token, conv_id, a1_id)
    a1b_id = regen["message_id"]

    # Sanity: after the regen the active path is [U1, A1b]; the set-aside tail is gone.
    db_session.expire_all()
    a1_pre = (
        await db_session.execute(select(Message).where(Message.id == _uuid.UUID(a1_id)))
    ).scalar_one()
    a2_pre = (
        await db_session.execute(select(Message).where(Message.id == _uuid.UUID(a2_id)))
    ).scalar_one()
    assert a1_pre.is_active is False
    assert a2_pre.is_active is False

    # Switch the PAST turn back to A1 — its set-aside continuation must return.
    status, path = await _switch(http_client, token, conv_id, a1_id)
    assert status == 200, path

    # The rendered path is the whole original branch [U1, A1, U2, A2] again.
    path_ids = [m["id"] for m in path]
    u1_id = path_ids[0]
    assert path_ids == [u1_id, a1_id, str(u2_id), a2_id]
    assert [m["content"] for m in path] == [
        "Вопрос1", "Ответ1", "Вопрос2", "Ответ2",
    ]

    # DB: A1/U2/A2 active again, A1b set aside (deactivated, NOT deleted).
    db_session.expire_all()
    a1_row = (
        await db_session.execute(select(Message).where(Message.id == _uuid.UUID(a1_id)))
    ).scalar_one()
    a2_row = (
        await db_session.execute(select(Message).where(Message.id == _uuid.UUID(a2_id)))
    ).scalar_one()
    a1b_row = (
        await db_session.execute(select(Message).where(Message.id == _uuid.UUID(a1b_id)))
    ).scalar_one()
    assert a1_row.is_active is True
    assert a2_row.is_active is True
    assert a1b_row.is_active is False
    assert a1b_row.deleted_at is None  # set aside, never deleted

    leaf = await _assert_single_active_leaf(db_session, conv_uuid)
    assert leaf == _uuid.UUID(a2_id)


async def test_switch_to_node_with_inactive_ancestors_single_leaf(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BUG 1: switching to a node whose ANCESTORS are inactive must re-activate
    its full ancestor chain AND deactivate the previously-active branch — exactly
    ONE active leaf, never two.

    Repro:
        send turn1 (U1 ─ A1), send turn2 (U2 ─ A2) chained off A1.
        edit U1 → a NEW user sibling U1e + its forced assistant A1e become active;
            U1, A1, U2, A2 are all set aside (the active path is now [U1e, A1e]).
        PUT /active-path {node_id: A2}.

    A2's ancestors (U1, A1, U2) are inactive. The OLD switch only deactivated the
    active sibling at A2's OWN fork (none, since A2 is inactive) then activated
    A2 — leaving U1e/A1e active too: TWO active leaves, A2's chain non-contiguous.
    The fix rewrites the whole active path to the single chain through A2.
    """
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)
    conv_uuid = _uuid.UUID(conv_id)

    # Turn 1: U1 ─ A1.
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ1"),
        StreamChunk(type="usage", input_tokens=4, output_tokens=2),
        StreamChunk(type="done"),
    ])
    t1 = await _send_turn(http_client, token, conv_id, "Вопрос1")
    a1_id = t1["message_id"]

    # Turn 2: U2 ─ A2 (chained off A1).
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ2"),
        StreamChunk(type="usage", input_tokens=4, output_tokens=2),
        StreamChunk(type="done"),
    ])
    t2 = await _send_turn(http_client, token, conv_id, "Вопрос2")
    a2_id = t2["message_id"]

    # Capture U1/U2 ids before any expire/mutation.
    db_session.expire_all()
    u1 = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conv_uuid,
                Message.role == "user",
                Message.content == "Вопрос1",
            )
        )
    ).scalar_one()
    u2 = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conv_uuid,
                Message.role == "user",
                Message.content == "Вопрос2",
            )
        )
    ).scalar_one()
    u1_id, u2_id = u1.id, u2.id

    # Edit U1 → new user sibling U1e + forced assistant A1e; U1/A1/U2/A2 set aside.
    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ1e"),
        StreamChunk(type="usage", input_tokens=4, output_tokens=2),
        StreamChunk(type="done"),
    ])
    edit_done = await _edit(http_client, token, conv_id, str(u1_id), "Вопрос1-ред")
    a1e_id = edit_done["message_id"]

    # Sanity: after the edit the active path is [U1e, A1e]; A2's chain is inactive.
    db_session.expire_all()
    a2_pre = (
        await db_session.execute(select(Message).where(Message.id == _uuid.UUID(a2_id)))
    ).scalar_one()
    assert a2_pre.is_active is False
    pre_leaf = await _assert_single_active_leaf(db_session, conv_uuid)
    assert pre_leaf == _uuid.UUID(a1e_id)

    # Switch to A2 — its inactive ancestors (U1, A1, U2) must re-activate and the
    # edit branch (U1e, A1e) must deactivate. EXACTLY one active leaf.
    status, path = await _switch(http_client, token, conv_id, a2_id)
    assert status == 200, path

    # The rendered path is the coherent original chain [U1, A1, U2, A2].
    path_ids = [m["id"] for m in path]
    assert path_ids == [str(u1_id), a1_id, str(u2_id), a2_id]

    # DB invariant: exactly one active leaf == A2, single contiguous active path.
    db_session.expire_all()
    leaf = await _assert_single_active_leaf(db_session, conv_uuid)
    assert leaf == _uuid.UUID(a2_id)

    # The edit branch is set aside (deactivated, not deleted).
    a1e_row = (
        await db_session.execute(select(Message).where(Message.id == _uuid.UUID(a1e_id)))
    ).scalar_one()
    assert a1e_row.is_active is False
    assert a1e_row.deleted_at is None
