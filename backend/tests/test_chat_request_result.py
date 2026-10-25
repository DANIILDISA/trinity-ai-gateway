"""GET /api/v1/chat/conversations/{id}/requests/{request_id} (tr-sgz).

Tests the post-Stop reconciliation endpoint that returns a message_done-shaped
response so the client can fill in cost/tokens for a partial assistant message
without a full page reload.
"""
import uuid as _uuid

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import Message
from app.models.notification import Notification
from app.models.reservation import Reservation
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User
from app.services.billing_service import credit_balance

REGISTER_URL = "/api/v1/auth/register"
CONV_URL = "/api/v1/chat/conversations"
MODEL_ID = "openai/gpt-4.1-mini"

_TEST_EMAILS = [
    "req_result_a@example.com",
    "req_result_b@example.com",
]


# ── Fixtures ─────────────────────────────────────────────────────────────────


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
        await db_session.execute(
            sa_delete(Message).where(Message.conversation_id.in_(conv_ids))
        )
        await db_session.execute(
            sa_delete(Conversation).where(Conversation.user_id == uid)
        )
        await db_session.execute(
            sa_delete(UsageLog).where(UsageLog.user_id == uid)
        )
        await db_session.execute(
            sa_delete(Reservation).where(Reservation.user_id == uid)
        )
        await db_session.execute(
            sa_delete(Transaction).where(Transaction.user_id == uid)
        )
        await db_session.execute(
            sa_delete(Notification).where(Notification.user_id == uid)
        )
        await db_session.delete(user)
    await db_session.commit()


async def _register(http_client: AsyncClient, email: str) -> tuple[str, str]:
    resp = await http_client.post(
        REGISTER_URL,
        json={"email": email, "password": "SecurePass1", "display_name": "Тестер"},
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
        idempotency_key=f"rr-seed-{user_id_str}-{_uuid.uuid4()}",
        description="request-result test seed",
    )
    await db.commit()


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_returns_message_done_shape_for_persisted_assistant(
    http_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Happy path: GET with a known request_id on an owned conversation returns
    200 with message_id, request_id, actual_cost, and tokens_used keys."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 10_000_000_000)

    # Create a conversation via the API (so it's owned by the authed user).
    resp = await http_client.post(
        CONV_URL,
        json={"model_id": MODEL_ID, "title": "Тест запроса"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    conv_id = resp.json()["id"]

    # Directly insert an assistant message with a known request_id.
    request_id = _uuid.uuid4()
    msg = Message(
        conversation_id=_uuid.UUID(conv_id),
        role="assistant",
        content="частичный ответ",
        model_id=MODEL_ID,
        request_id=request_id,
        cost_micro_rub=5_000,
        input_tokens=10,
        output_tokens=7,
        reasoning_tokens=3,
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)
    msg_id = msg.id  # capture before expire

    # GET the request result.
    resp = await http_client.get(
        f"{CONV_URL}/{conv_id}/requests/{request_id}",
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["message_id"] == str(msg_id)
    assert body["request_id"] == str(request_id)
    assert "actual_cost" in body
    assert isinstance(body["actual_cost"], str)
    tokens = body["tokens_used"]
    assert tokens["input"] == 10
    assert tokens["output"] == 7
    assert tokens["reasoning"] == 3
    # No usage_log for this directly-inserted message ⇒ not stopped.
    assert body["stopped"] is False


def _usage_log(conv_id: str, request_id, status: str) -> UsageLog:
    """Minimal usage_log row for the request-result reconcile, with the status
    under test. Numeric/cost fields are arbitrary — only `status` is asserted."""
    return UsageLog(
        user_id=None,  # set by caller
        request_id=request_id,
        conversation_id=_uuid.UUID(conv_id),
        model_id=MODEL_ID,
        input_tokens=10,
        output_tokens=7,
        reasoning_tokens=3,
        estimated_input_tokens=10,
        estimated_output_tokens=7,
        estimated_cost_micro_rub=5_000,
        actual_cost_micro_rub=5_000,
        reserved_micro_rub=5_000,
        openrouter_cost_micro_usd=100,
        exchange_rate_micro_rub=90_000_000,
        status=status,
    )


async def test_result_endpoint_marks_partial_as_stopped(
    http_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Post-Stop reconcile (tr-fo1): a request whose usage_log finalized as
    'partial' returns stopped=true so the live bubble flips to «Остановлено»
    without a reload; a 'completed' usage_log returns stopped=false (the read-path
    derivation, no finalize change). GUARD: only 'partial' counts."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 10_000_000_000)

    resp = await http_client.post(
        CONV_URL,
        json={"model_id": MODEL_ID, "title": "Стоп-реконсайл"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    conv_id = resp.json()["id"]

    async def _make(status: str) -> tuple[str, str]:
        request_id = _uuid.uuid4()
        msg = Message(
            conversation_id=_uuid.UUID(conv_id),
            role="assistant",
            content=f"ответ {status}",
            model_id=MODEL_ID,
            request_id=request_id,
            cost_micro_rub=5_000,
            input_tokens=10,
            output_tokens=7,
            reasoning_tokens=3,
        )
        ulog = _usage_log(conv_id, request_id, status)
        ulog.user_id = _uuid.UUID(user_id)
        db_session.add_all([msg, ulog])
        await db_session.commit()
        return str(request_id), status

    partial_rid, _ = await _make("partial")
    completed_rid, _ = await _make("completed")

    r_partial = await http_client.get(
        f"{CONV_URL}/{conv_id}/requests/{partial_rid}", headers=_auth(token)
    )
    assert r_partial.status_code == 200, r_partial.text
    assert r_partial.json()["stopped"] is True

    r_completed = await http_client.get(
        f"{CONV_URL}/{conv_id}/requests/{completed_rid}", headers=_auth(token)
    )
    assert r_completed.status_code == 200, r_completed.text
    assert r_completed.json()["stopped"] is False


async def test_result_endpoint_returns_thinking_segments(
    http_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Post-Stop reconcile (tr-cy0.3.7): the result endpoint surfaces the
    persisted per-segment reasoning so titles survive a Stop without a reload."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 10_000_000_000)

    resp = await http_client.post(
        CONV_URL,
        json={"model_id": MODEL_ID, "title": "Сегменты"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    conv_id = resp.json()["id"]

    request_id = _uuid.uuid4()
    segments = [
        {"id": "seg-0", "text": "Анализ", "summary": "Анализирую вопросы"},
        {"id": "seg-1", "text": "Ответ", "summary": "Готовлю ответ"},
    ]
    msg = Message(
        conversation_id=_uuid.UUID(conv_id),
        role="assistant",
        content="частичный ответ",
        model_id=MODEL_ID,
        request_id=request_id,
        cost_micro_rub=5_000,
        input_tokens=10,
        output_tokens=7,
        reasoning_tokens=3,
        thinking="Анализ\nОтвет",
        thinking_segments=segments,
    )
    db_session.add(msg)
    await db_session.commit()

    resp = await http_client.get(
        f"{CONV_URL}/{conv_id}/requests/{request_id}",
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["thinking_segments"] == segments


async def test_result_endpoint_returns_tool_steps(
    http_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Post-Stop reconcile (tr-cy0.2.7/2.16 §8): the result endpoint surfaces the
    persisted agentic tool steps so the timeline + artifact card survive a Stop
    without a reload."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 10_000_000_000)

    resp = await http_client.post(
        CONV_URL,
        json={"model_id": MODEL_ID, "title": "Шаги"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    conv_id = resp.json()["id"]

    request_id = _uuid.uuid4()
    tool_steps = [
        {
            "id": "tc-0-0",
            "tool": "create_file",
            "title": "Создание файла a.md",
            "filename": "a.md",
            "diff": {"added": 2, "removed": 0},
            "presented": False,
            "status": "done",
            "output": "ok",
        }
    ]
    msg = Message(
        conversation_id=_uuid.UUID(conv_id),
        role="assistant",
        content="готово",
        model_id=MODEL_ID,
        request_id=request_id,
        cost_micro_rub=5_000,
        input_tokens=10,
        output_tokens=7,
        reasoning_tokens=0,
        tool_steps=tool_steps,
    )
    db_session.add(msg)
    await db_session.commit()

    resp = await http_client.get(
        f"{CONV_URL}/{conv_id}/requests/{request_id}",
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    steps = resp.json()["tool_steps"]
    assert len(steps) == 1
    assert steps[0]["tool"] == "create_file"
    assert steps[0]["filename"] == "a.md"
    assert steps[0]["status"] == "done"
    assert steps[0]["diff"] == {"added": 2, "removed": 0}


async def test_404_when_request_not_persisted(
    http_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """GET with an unknown request_id on an owned conversation returns 404."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 10_000_000_000)

    resp = await http_client.post(
        CONV_URL,
        json={"model_id": MODEL_ID, "title": "Тест 404"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    conv_id = resp.json()["id"]

    unknown_request_id = _uuid.uuid4()
    resp = await http_client.get(
        f"{CONV_URL}/{conv_id}/requests/{unknown_request_id}",
        headers=_auth(token),
    )
    assert resp.status_code == 404


async def test_404_for_other_users_conversation(
    http_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """GET on a conversation owned by another user returns 404 (no existence leak)."""
    # Register two users.
    token_a, user_id_a = await _register(http_client, _TEST_EMAILS[0])
    token_b, user_id_b = await _register(http_client, _TEST_EMAILS[1])
    await _seed_balance(db_session, user_id_a, 10_000_000_000)

    # User A creates a conversation and a message.
    resp = await http_client.post(
        CONV_URL,
        json={"model_id": MODEL_ID, "title": "Чужой диалог"},
        headers=_auth(token_a),
    )
    assert resp.status_code == 200, resp.text
    conv_id_a = resp.json()["id"]

    request_id = _uuid.uuid4()
    msg = Message(
        conversation_id=_uuid.UUID(conv_id_a),
        role="assistant",
        content="ответ А",
        model_id=MODEL_ID,
        request_id=request_id,
        cost_micro_rub=1_000,
        input_tokens=5,
        output_tokens=2,
        reasoning_tokens=0,
    )
    db_session.add(msg)
    await db_session.commit()

    # User B tries to access User A's conversation — must get 404.
    resp = await http_client.get(
        f"{CONV_URL}/{conv_id_a}/requests/{request_id}",
        headers=_auth(token_b),
    )
    assert resp.status_code == 404


async def test_artifact_download_endpoint(
    http_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """GET …/artifact?filename= returns the latest version; 404 for unknown."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 10_000_000_000)
    resp = await http_client.post(
        CONV_URL, json={"model_id": MODEL_ID, "title": "Арт"}, headers=_auth(token)
    )
    conv_id = resp.json()["id"]

    from app.models.artifact import Artifact

    art = Artifact(
        user_id=_uuid.UUID(user_id), conversation_id=_uuid.UUID(conv_id),
        group_id=_uuid.uuid4(), version=1, filename="a.md",
        media_type="text/markdown", content="# Hi\nbody", size_bytes=10,
    )
    db_session.add(art)
    await db_session.commit()

    r = await http_client.get(
        f"{CONV_URL}/{conv_id}/artifact?filename=a.md", headers=_auth(token)
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "filename": "a.md", "media_type": "text/markdown", "content": "# Hi\nbody",
        "version": 1, "version_count": 1,
    }
    r2 = await http_client.get(
        f"{CONV_URL}/{conv_id}/artifact?filename=missing.md", headers=_auth(token)
    )
    assert r2.status_code == 404


async def test_artifact_versions_endpoint(
    http_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A str_replace adds a v2 row under the same group_id. GET (default) returns
    the latest version with version=2/version_count=2; ?version=1 returns v1's
    content; an out-of-range version is 404."""
    token, user_id = await _register(http_client, _TEST_EMAILS[0])
    await _seed_balance(db_session, user_id, 10_000_000_000)
    resp = await http_client.post(
        CONV_URL, json={"model_id": MODEL_ID, "title": "Версии"}, headers=_auth(token)
    )
    conv_id = resp.json()["id"]

    from app.models.artifact import Artifact

    group_id = _uuid.uuid4()
    db_session.add(
        Artifact(
            user_id=_uuid.UUID(user_id), conversation_id=_uuid.UUID(conv_id),
            group_id=group_id, version=1, filename="roadmap.md",
            media_type="text/markdown", content="v1 body", size_bytes=7,
        )
    )
    db_session.add(
        Artifact(
            user_id=_uuid.UUID(user_id), conversation_id=_uuid.UUID(conv_id),
            group_id=group_id, version=2, filename="roadmap.md",
            media_type="text/markdown", content="v2 body", size_bytes=7,
        )
    )
    await db_session.commit()

    # Default = latest version, with version metadata.
    latest = await http_client.get(
        f"{CONV_URL}/{conv_id}/artifact?filename=roadmap.md", headers=_auth(token)
    )
    assert latest.status_code == 200, latest.text
    assert latest.json() == {
        "filename": "roadmap.md", "media_type": "text/markdown",
        "content": "v2 body", "version": 2, "version_count": 2,
    }

    # Explicit older version returns that version's content.
    v1 = await http_client.get(
        f"{CONV_URL}/{conv_id}/artifact?filename=roadmap.md&version=1",
        headers=_auth(token),
    )
    assert v1.status_code == 200, v1.text
    assert v1.json() == {
        "filename": "roadmap.md", "media_type": "text/markdown",
        "content": "v1 body", "version": 1, "version_count": 2,
    }

    # Out-of-range version is a 404.
    missing = await http_client.get(
        f"{CONV_URL}/{conv_id}/artifact?filename=roadmap.md&version=9",
        headers=_auth(token),
    )
    assert missing.status_code == 404
