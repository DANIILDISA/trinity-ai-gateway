"""Version-history read-path tests: active-path GET + ?include=all (Task 9)
plus the all-branch branch_totals aggregate (Task 13)."""
import uuid as _uuid
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import Message

REGISTER_URL = "/api/v1/auth/register"
CONV_URL = "/api/v1/chat/conversations"
MODEL_ID = "openai/gpt-4.1-mini"


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


async def _make_conv(http_client: AsyncClient, token: str) -> _uuid.UUID:
    create = await http_client.post(
        CONV_URL, json={"model_id": MODEL_ID, "title": "Ветки"}, headers=_auth(token)
    )
    assert create.status_code == 200, create.text
    return _uuid.UUID(create.json()["id"])


async def _branched_tree(
    db_session: AsyncSession, conv_id: _uuid.UUID
) -> dict[str, _uuid.UUID]:
    """U1 -> A1(v1, inactive) / A2(v2, active). Active leaf = A2."""
    base = datetime.now(timezone.utc)
    u1 = Message(
        conversation_id=conv_id,
        role="user",
        content="Вопрос",
        is_active=True,
        created_at=base,
    )
    db_session.add(u1)
    await db_session.flush()
    a1 = Message(
        conversation_id=conv_id,
        role="assistant",
        content="Ответ один",
        model_id=MODEL_ID,
        input_tokens=10,
        output_tokens=20,
        cost_micro_rub=5000,
        request_id=_uuid.uuid4(),
        parent_id=u1.id,
        is_active=False,
        created_at=base + timedelta(milliseconds=10),
    )
    a2 = Message(
        conversation_id=conv_id,
        role="assistant",
        content="Ответ два",
        model_id=MODEL_ID,
        input_tokens=11,
        output_tokens=22,
        cost_micro_rub=6000,
        request_id=_uuid.uuid4(),
        parent_id=u1.id,
        is_active=True,
        created_at=base + timedelta(milliseconds=20),
    )
    db_session.add_all([a1, a2])
    await db_session.flush()
    conv = (
        await db_session.execute(
            select(Conversation).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    conv.active_leaf_id = a2.id
    # Capture ids into plain vars BEFORE commit/expire (avoid MissingGreenlet).
    ids = {"u1": u1.id, "a1": a1.id, "a2": a2.id}
    await db_session.commit()
    return ids


async def test_get_returns_only_active_path_with_version_count(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, _ = await _register(http_client, "ver_a@example.com")
    conv_id = await _make_conv(http_client, token)
    ids = await _branched_tree(db_session, conv_id)

    resp = await http_client.get(f"{CONV_URL}/{conv_id}", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    msgs = resp.json()["messages"]
    # Active path only: U1 + A2 (A1 is inactive).
    assert [m["id"] for m in msgs] == [str(ids["u1"]), str(ids["a2"])]
    assistant = msgs[1]
    assert assistant["parent_id"] == str(ids["u1"])
    assert assistant["is_active"] is True
    # A2 is version 2 of 2 (1-based index, siblings share parent_id+role).
    assert assistant["version_index"] == 2
    assert assistant["version_count"] == 2
    # The user turn is a single version.
    assert msgs[0]["version_index"] == 1
    assert msgs[0]["version_count"] == 1


async def test_get_include_all_returns_inactive_siblings(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, _ = await _register(http_client, "ver_b@example.com")
    conv_id = await _make_conv(http_client, token)
    ids = await _branched_tree(db_session, conv_id)

    resp = await http_client.get(
        f"{CONV_URL}/{conv_id}?include=all", headers=_auth(token)
    )
    assert resp.status_code == 200, resp.text
    returned = {m["id"] for m in resp.json()["messages"]}
    # All three nodes incl. the inactive A1 sibling.
    assert returned == {str(ids["u1"]), str(ids["a1"]), str(ids["a2"])}
    by_id = {m["id"]: m for m in resp.json()["messages"]}
    assert by_id[str(ids["a1"])]["is_active"] is False
    assert by_id[str(ids["a1"])]["version_index"] == 1
    assert by_id[str(ids["a1"])]["version_count"] == 2


async def test_get_legacy_null_active_leaf_falls_back_to_created_at(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, _ = await _register(http_client, "ver_c@example.com")
    conv_id = await _make_conv(http_client, token)
    # Legacy rows: parent_id NULL, active_leaf_id NULL, is_active default true.
    base = datetime.now(timezone.utc)
    db_session.add(
        Message(
            conversation_id=conv_id,
            role="user",
            content="Привет",
            created_at=base,
        )
    )
    db_session.add(
        Message(
            conversation_id=conv_id,
            role="assistant",
            content="Привет!",
            model_id=MODEL_ID,
            input_tokens=2,
            output_tokens=2,
            cost_micro_rub=100,
            request_id=_uuid.uuid4(),
            created_at=base + timedelta(milliseconds=10),
        )
    )
    await db_session.commit()

    resp = await http_client.get(f"{CONV_URL}/{conv_id}", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    msgs = resp.json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    # Legacy turns are single-version everywhere → switcher auto-hides.
    assert all(m["version_count"] == 1 for m in msgs)
    assert all(m["version_index"] == 1 for m in msgs)
    assert all(m["parent_id"] is None for m in msgs)


async def test_branch_totals_sums_across_all_branches(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, _ = await _register(http_client, "ver_d@example.com")
    conv_id = await _make_conv(http_client, token)
    # Reuse the U1 -> A1(inactive) / A2(active) tree from Task 9 helper.
    await _branched_tree(db_session, conv_id)

    resp = await http_client.get(f"{CONV_URL}/{conv_id}", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    totals = resp.json()["branch_totals"]
    # Spend/tokens summed over BOTH assistant generations (active A2 + inactive A1).
    assert totals["total_cost_micro_rub"] == 5000 + 6000
    assert totals["total_input_tokens"] == 10 + 11
    assert totals["total_output_tokens"] == 20 + 22
    assert totals["total_reasoning_tokens"] == 0  # both NULL → coalesced to 0
    # 3 messages total (U1 + A1 + A2); 2 assistant generations.
    assert totals["message_count"] == 3
    assert totals["generation_count"] == 2
    # The active-path GET still shows exactly one assistant.
    msgs = resp.json()["messages"]
    assert sum(1 for m in msgs if m["role"] == "assistant") == 1


async def test_branch_totals_present_on_empty_conversation(
    http_client: AsyncClient,
) -> None:
    token, _ = await _register(http_client, "ver_e@example.com")
    conv_id = await _make_conv(http_client, token)
    resp = await http_client.get(f"{CONV_URL}/{conv_id}", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    totals = resp.json()["branch_totals"]
    assert totals == {
        "total_cost_micro_rub": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_reasoning_tokens": 0,
        "message_count": 0,
        "generation_count": 0,
    }


async def test_branch_totals_absent_on_list_and_create(
    http_client: AsyncClient,
) -> None:
    token, _ = await _register(http_client, "ver_f@example.com")
    create = await http_client.post(
        CONV_URL, json={"model_id": MODEL_ID}, headers=_auth(token)
    )
    # POST/list use response_model_exclude_none=True → branch_totals dropped.
    assert "branch_totals" not in create.json()
    lst = await http_client.get(CONV_URL, headers=_auth(token))
    assert all("branch_totals" not in item for item in lst.json()["items"])
