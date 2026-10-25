"""End-to-end tests for POST /api/v1/chat/conversations/{id}/messages (Task 6.3)."""
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
OTHER_MODEL_ID = "anthropic/claude-haiku-4-5"

_TEST_EMAILS = ["sse_test_a@example.com", "sse_test_b@example.com"]


# ── Fixtures ────────────────────────────────────────────────────────────────


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
        await db_session.execute(sa_delete(Message).where(
            Message.conversation_id.in_(
                select(Conversation.id).where(Conversation.user_id == uid)
            )
        ))
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


async def _seed_balance(
    db: AsyncSession, user_id_str: str, amount_micro_rub: int
) -> None:
    await credit_balance(
        db,
        user_id=_uuid.UUID(user_id_str),
        amount_micro_rub=amount_micro_rub,
        idempotency_key=f"sse-seed-{user_id_str}-{_uuid.uuid4()}",
        description="SSE test seed",
    )
    await db.commit()


async def _create_conversation(
    http_client: AsyncClient, token: str, model_id: str = MODEL_ID
) -> str:
    resp = await http_client.post(
        CONV_URL,
        json={"model_id": model_id, "title": "Тест"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _install_proxy(
    monkeypatch: pytest.MonkeyPatch,
    chunks_or_exception,
) -> None:
    """Patch OpenRouterProxy at its source module so process_chat_request's
    local import picks up the fake (see billing_service.process_chat_request).

    Also force DEMO_MODE off — otherwise the orchestrator picks DemoProxy
    instead of OpenRouterProxy and ignores this patch entirely (Task 7.1).
    """

    class _FakeProxy:
        def __init__(self, db):
            self.db = db

        async def proxy_to_openrouter_with_retry(
            self, messages, model_id, max_retries=3, plugins=None, reasoning_budget=None
        ):
            if isinstance(chunks_or_exception, BaseException):
                raise chunks_or_exception
            for c in chunks_or_exception:
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


# ── Tests ───────────────────────────────────────────────────────────────────


async def test_sse_stream_returns_events(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token)

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="Ответ "),
        StreamChunk(type="content", delta="готов."),
        StreamChunk(type="usage", input_tokens=12, output_tokens=4),
        StreamChunk(type="done"),
    ])

    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages",
        json={"content": "Привет", "model": MODEL_ID},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(resp.text)
    names = [e["event"] for e in events]
    assert names[0] == "message_start"
    assert "content_delta" in names
    assert names[-1] == "message_done"

    done = next(e for e in events if e["event"] == "message_done")
    assert "actual_cost" in done["data"]
    assert "balance_after" in done["data"]
    assert done["data"]["tokens_used"] == {"input": 12, "output": 4, "reasoning": 0}


async def test_user_message_saved_before_stream(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id_str = await _create_conversation(http_client, token)
    conv_id = _uuid.UUID(conv_id_str)

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="hi"),
        StreamChunk(type="usage", input_tokens=2, output_tokens=1),
        StreamChunk(type="done"),
    ])

    resp = await http_client.post(
        f"{CONV_URL}/{conv_id_str}/messages",
        json={"content": "Привет, бот", "model": MODEL_ID},
        headers=_auth(token),
    )
    assert resp.status_code == 200

    db_session.expire_all()
    msgs = (
        await db_session.execute(
            select(Message)
            .where(Message.conversation_id == conv_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    ).scalars().all()
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].content == "Привет, бот"
    # The user message must precede the assistant message in time.
    assert msgs[0].created_at <= msgs[1].created_at
    # Assistant message carries billing data.
    assert msgs[1].input_tokens == 2
    assert msgs[1].output_tokens == 1
    assert msgs[1].cost_micro_rub is not None and msgs[1].cost_micro_rub > 0


async def test_two_sends_keep_single_active_leaf_via_endpoint(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real endpoint, two back-to-back sends: the §0.1 user-save critical
    section (lock → derive parent from locked active path → save → advance leaf)
    keeps a single, linear active path. After two turns the second user node
    parents off the first assistant (the prior active leaf) and exactly one
    active leaf survives (the latest assistant)."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id_str = await _create_conversation(http_client, token)
    conv_id = _uuid.UUID(conv_id_str)

    def _turn():
        return [
            StreamChunk(type="content", delta="ок"),
            StreamChunk(type="usage", input_tokens=3, output_tokens=1),
            StreamChunk(type="done"),
        ]

    _install_proxy(monkeypatch, _turn())
    r1 = await http_client.post(
        f"{CONV_URL}/{conv_id_str}/messages",
        json={"content": "первый", "model": MODEL_ID},
        headers=_auth(token),
    )
    assert r1.status_code == 200
    # Drain the SSE body so the stream (and its commit) fully completes.
    _ = r1.text

    _install_proxy(monkeypatch, _turn())
    r2 = await http_client.post(
        f"{CONV_URL}/{conv_id_str}/messages",
        json={"content": "второй", "model": MODEL_ID},
        headers=_auth(token),
    )
    assert r2.status_code == 200
    _ = r2.text

    db_session.expire_all()
    msgs = (
        await db_session.execute(
            select(Message)
            .where(Message.conversation_id == conv_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    ).scalars().all()
    assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]
    u1, a1, u2, a2 = msgs
    # First user node is the root; second user node parents off the first
    # assistant (the active leaf at the time the second send ran under lock).
    assert u1.parent_id is None
    assert u2.parent_id == a1.id
    # Exactly one active leaf, equal to active_leaf_id (the latest assistant).
    active = [m for m in msgs if m.is_active]
    active_ids = {m.id for m in active}
    parents = {m.parent_id for m in active if m.parent_id is not None}
    leaves = active_ids - parents
    leaf = (
        await db_session.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    assert leaves == {leaf} == {a2.id}


async def test_duplicate_client_request_id_returns_409_no_double_charge(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend idempotency gate (tr-n5b): two POSTs carrying the SAME
    client_request_id within the dedup window — the FIRST proceeds, the SECOND
    returns a clean 409 `duplicate_request` and creates NO second user message,
    NO second usage_log, NO second reservation (defense-in-depth against a
    multi-tab / HTTP-retry / programmatic duplicate of the SAME logical send)."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id_str = await _create_conversation(http_client, token)
    conv_id = _uuid.UUID(conv_id_str)
    uid = _uuid.UUID(user_id)

    def _turn():
        return [
            StreamChunk(type="content", delta="ок"),
            StreamChunk(type="usage", input_tokens=3, output_tokens=1),
            StreamChunk(type="done"),
        ]

    crid = str(_uuid.uuid4())

    _install_proxy(monkeypatch, _turn())
    r1 = await http_client.post(
        f"{CONV_URL}/{conv_id_str}/messages",
        json={"content": "дубль", "model": MODEL_ID, "client_request_id": crid},
        headers=_auth(token),
    )
    assert r1.status_code == 200, r1.text
    # Drain the SSE body so the first stream + its billing commit fully complete.
    _ = r1.text

    _install_proxy(monkeypatch, _turn())
    r2 = await http_client.post(
        f"{CONV_URL}/{conv_id_str}/messages",
        json={"content": "дубль", "model": MODEL_ID, "client_request_id": crid},
        headers=_auth(token),
    )
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"]["code"] == "duplicate_request"

    db_session.expire_all()
    msgs = (
        await db_session.execute(
            select(Message)
            .where(Message.conversation_id == conv_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    ).scalars().all()
    # Exactly one turn persisted — the duplicate did not write a second
    # user/assistant pair.
    assert [m.role for m in msgs] == ["user", "assistant"]

    usage_count = (
        await db_session.execute(
            select(func.count()).select_from(UsageLog).where(UsageLog.user_id == uid)
        )
    ).scalar_one()
    assert usage_count == 1

    reservation_count = (
        await db_session.execute(
            select(func.count())
            .select_from(Reservation)
            .where(Reservation.user_id == uid)
        )
    ).scalar_one()
    assert reservation_count == 1


async def test_idempotency_redis_oom_returns_503_no_write(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    """tr-5rd7: an OOM ResponseError on the idempotency SET must fail CLOSED with a
    retryable 503 (NOT 500, NOT fail-open) — no user message / reservation created."""
    from redis.exceptions import ResponseError

    from app.dependencies import get_redis
    from app.main import app

    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id_str = await _create_conversation(http_client, token)
    uid = _uuid.UUID(user_id)

    class _OOMRedis:
        async def set(self, *a, **k):
            raise ResponseError("OOM command not allowed when used memory > 'maxmemory'")
        async def exists(self, *a, **k):  # blocklist READ in the auth chain (not denyoom)
            return 0

    async def _fake_get_redis():
        yield _OOMRedis()

    app.dependency_overrides[get_redis] = _fake_get_redis
    try:
        r = await http_client.post(
            f"{CONV_URL}/{conv_id_str}/messages",
            json={"content": "проверка", "model": MODEL_ID, "client_request_id": str(_uuid.uuid4())},
            headers=_auth(token),
        )
        assert r.status_code == 503, r.text
    finally:
        del app.dependency_overrides[get_redis]

    db_session.expire_all()
    msg_count = (
        await db_session.execute(
            select(func.count()).select_from(Message).where(
                Message.conversation_id == _uuid.UUID(conv_id_str)
            )
        )
    ).scalar_one()
    assert msg_count == 0
    res_count = (
        await db_session.execute(
            select(func.count()).select_from(Reservation).where(Reservation.user_id == uid)
        )
    ).scalar_one()
    assert res_count == 0


async def test_different_client_request_ids_both_proceed(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two sends with DIFFERENT client_request_ids are independent logical sends —
    both proceed (no false dedup)."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id_str = await _create_conversation(http_client, token)
    conv_id = _uuid.UUID(conv_id_str)

    def _turn():
        return [
            StreamChunk(type="content", delta="ок"),
            StreamChunk(type="usage", input_tokens=3, output_tokens=1),
            StreamChunk(type="done"),
        ]

    _install_proxy(monkeypatch, _turn())
    r1 = await http_client.post(
        f"{CONV_URL}/{conv_id_str}/messages",
        json={"content": "первый", "model": MODEL_ID, "client_request_id": str(_uuid.uuid4())},
        headers=_auth(token),
    )
    assert r1.status_code == 200, r1.text
    _ = r1.text

    _install_proxy(monkeypatch, _turn())
    r2 = await http_client.post(
        f"{CONV_URL}/{conv_id_str}/messages",
        json={"content": "второй", "model": MODEL_ID, "client_request_id": str(_uuid.uuid4())},
        headers=_auth(token),
    )
    assert r2.status_code == 200, r2.text
    _ = r2.text

    db_session.expire_all()
    msgs = (
        await db_session.execute(
            select(Message)
            .where(Message.conversation_id == conv_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    ).scalars().all()
    assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]


async def test_no_client_request_id_proceeds_backcompat(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older client that omits client_request_id behaves EXACTLY as today —
    no dedup gate, full backward compatibility."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id_str = await _create_conversation(http_client, token)
    conv_id = _uuid.UUID(conv_id_str)

    def _turn():
        return [
            StreamChunk(type="content", delta="ок"),
            StreamChunk(type="usage", input_tokens=3, output_tokens=1),
            StreamChunk(type="done"),
        ]

    _install_proxy(monkeypatch, _turn())
    r1 = await http_client.post(
        f"{CONV_URL}/{conv_id_str}/messages",
        json={"content": "первый", "model": MODEL_ID},
        headers=_auth(token),
    )
    assert r1.status_code == 200, r1.text
    _ = r1.text

    _install_proxy(monkeypatch, _turn())
    r2 = await http_client.post(
        f"{CONV_URL}/{conv_id_str}/messages",
        json={"content": "второй", "model": MODEL_ID},
        headers=_auth(token),
    )
    assert r2.status_code == 200, r2.text
    _ = r2.text

    db_session.expire_all()
    msgs = (
        await db_session.execute(
            select(Message)
            .where(Message.conversation_id == conv_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    ).scalars().all()
    assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]


async def test_model_override_updates_conversation(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 1_000_000_000)
    conv_id_str = await _create_conversation(http_client, token, model_id=MODEL_ID)
    conv_id = _uuid.UUID(conv_id_str)

    _install_proxy(monkeypatch, [
        StreamChunk(type="content", delta="ok"),
        StreamChunk(type="usage", input_tokens=1, output_tokens=1),
        StreamChunk(type="done"),
    ])

    resp = await http_client.post(
        f"{CONV_URL}/{conv_id_str}/messages",
        json={"content": "Hi", "model": OTHER_MODEL_ID},
        headers=_auth(token),
    )
    assert resp.status_code == 200

    db_session.expire_all()
    conv = (
        await db_session.execute(
            select(Conversation).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    assert conv.model_id == OTHER_MODEL_ID


async def test_wrong_owner_returns_404(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_a, user_a = await _register(http_client, _TEST_EMAILS[0])
    token_b, _ = await _register(http_client, _TEST_EMAILS[1])
    await _seed_balance(db_session, user_a, 1_000_000_000)
    conv_id = await _create_conversation(http_client, token_a)

    # Even with the proxy installed, ownership rejects before any streaming.
    _install_proxy(monkeypatch, [StreamChunk(type="done")])

    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages",
        json={"content": "взлом", "model": MODEL_ID},
        headers=_auth(token_b),
    )
    assert resp.status_code == 404


async def test_insufficient_funds_returns_error_sse(
    http_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, _ = await _register(http_client, _TEST_EMAILS[0])
    # Balance = 0 → InsufficientFundsError fires inside create_reservation
    # BEFORE any stream chunk is yielded. The endpoint must catch it and
    # emit a single error SSE event (HTTP 200), not return an HTTP 4xx.
    conv_id = await _create_conversation(http_client, token)

    _install_proxy(monkeypatch, [StreamChunk(type="done")])

    resp = await http_client.post(
        f"{CONV_URL}/{conv_id}/messages",
        json={"content": "Не хватает", "model": MODEL_ID},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(resp.text)
    assert events, "expected at least one SSE event, got none"
    error_events = [e for e in events if e["event"] == "error"]
    assert len(error_events) == 1, f"expected 1 error event, got {events}"
    assert error_events[0]["data"]["code"] == "insufficient_funds"
