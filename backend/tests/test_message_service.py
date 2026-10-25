"""Tests for message_service (Task 6.2)."""
import asyncio
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.services.message_service import (
    get_conversation_messages,
    save_assistant_message,
    save_user_message,
)

TEST_EMAIL = "msg_service@example.com"
MODEL_ID = "openai/gpt-4.1-mini"


async def _purge(db: AsyncSession) -> None:
    user = (
        await db.execute(select(User).where(User.email_lower == TEST_EMAIL.lower()))
    ).scalar_one_or_none()
    if user is None:
        return
    await db.execute(sa_delete(Message).where(
        Message.conversation_id.in_(
            select(Conversation.id).where(Conversation.user_id == user.id)
        )
    ))
    await db.execute(sa_delete(Conversation).where(Conversation.user_id == user.id))
    await db.delete(user)
    await db.commit()


@pytest_asyncio.fixture
async def conversation(db_session: AsyncSession) -> Conversation:
    await _purge(db_session)
    user = User(
        email=TEST_EMAIL,
        display_name="Сообщение Тест",
        password_hash=None,
        role="user",
        status="active",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    conv = Conversation(user_id=user.id, model_id=MODEL_ID, title="Тест")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    yield conv
    db_session.expire_all()
    await _purge(db_session)


# ── 1. save_user_message ───────────────────────────────────────────────────


async def test_save_user_message(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    conv_id = conversation.id
    original_updated_at = conversation.updated_at

    # Brief sleep so updated_at provably advances.
    await asyncio.sleep(0.05)

    msg = await save_user_message(db_session, conv_id, "Привет, бот")
    await db_session.commit()

    assert msg.role == "user"
    assert msg.content == "Привет, бот"
    assert msg.model_id is None
    assert msg.input_tokens is None
    assert msg.output_tokens is None
    assert msg.cost_micro_rub is None
    assert msg.request_id is None

    db_session.expire_all()
    conv = (
        await db_session.execute(
            select(Conversation).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    assert conv.updated_at > original_updated_at


# ── 2. save_assistant_message ──────────────────────────────────────────────


async def test_save_assistant_message(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    conv_id = conversation.id
    req_id = _uuid.uuid4()

    msg = await save_assistant_message(
        db_session,
        conversation_id=conv_id,
        content="Привет, человек!",
        model_id=MODEL_ID,
        request_id=req_id,
        input_tokens=42,
        output_tokens=18,
        cost_micro_rub=12_345,
    )
    await db_session.commit()

    assert msg.role == "assistant"
    assert msg.content == "Привет, человек!"
    assert msg.model_id == MODEL_ID
    assert msg.request_id == req_id
    assert msg.input_tokens == 42
    assert msg.output_tokens == 18
    assert msg.cost_micro_rub == 12_345


# ── 3. get_conversation_messages: empty ─────────────────────────────────────


async def test_get_conversation_messages_empty(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    result = await get_conversation_messages(db_session, conversation.id)
    assert result == []


async def test_get_conversation_messages_none_id_returns_empty(
    db_session: AsyncSession,
) -> None:
    assert await get_conversation_messages(db_session, None) == []


# ── 4. get_conversation_messages: ordered ──────────────────────────────────


async def test_get_conversation_messages_ordered(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    conv_id = conversation.id
    # Postgres now() is per-transaction — set explicit timestamps to force order.
    base = datetime.now(timezone.utc)
    db_session.add(Message(
        conversation_id=conv_id,
        role="user",
        content="первое",
        created_at=base,
    ))
    db_session.add(Message(
        conversation_id=conv_id,
        role="assistant",
        content="ответ",
        model_id=MODEL_ID,
        input_tokens=3,
        output_tokens=2,
        cost_micro_rub=10,
        request_id=_uuid.uuid4(),
        created_at=base + timedelta(milliseconds=10),
    ))
    db_session.add(Message(
        conversation_id=conv_id,
        role="user",
        content="второе",
        created_at=base + timedelta(milliseconds=20),
    ))
    await db_session.commit()

    result = await get_conversation_messages(db_session, conv_id)
    assert result == [
        {"role": "user", "content": "первое"},
        {"role": "assistant", "content": "ответ"},
        {"role": "user", "content": "второе"},
    ]


# ── 5. system messages excluded ────────────────────────────────────────────


async def test_get_conversation_messages_skips_system(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    conv_id = conversation.id
    base = datetime.now(timezone.utc)
    db_session.add(Message(
        conversation_id=conv_id,
        role="system",
        content="System prompt — must not surface to the model context.",
        created_at=base,
    ))
    db_session.add(Message(
        conversation_id=conv_id,
        role="user",
        content="видимое",
        created_at=base + timedelta(milliseconds=10),
    ))
    await db_session.commit()

    result = await get_conversation_messages(db_session, conv_id)
    assert result == [{"role": "user", "content": "видимое"}]


# ── 6. both save functions bump updated_at ─────────────────────────────────


async def test_both_save_functions_bump_updated_at(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    conv_id = conversation.id
    started = conversation.updated_at

    await asyncio.sleep(0.05)
    await save_user_message(db_session, conv_id, "u")
    await db_session.commit()

    db_session.expire_all()
    after_user = (
        await db_session.execute(
            select(Conversation.updated_at).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    assert after_user > started

    await asyncio.sleep(0.05)
    await save_assistant_message(
        db_session,
        conversation_id=conv_id,
        content="a",
        model_id=MODEL_ID,
        request_id=_uuid.uuid4(),
        input_tokens=1,
        output_tokens=1,
        cost_micro_rub=1,
    )
    await db_session.commit()

    db_session.expire_all()
    after_assistant = (
        await db_session.execute(
            select(Conversation.updated_at).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    assert after_assistant > after_user


# ── 7. context reads return the ACTIVE BRANCH ONLY (Task 3) ────────────────
import uuid as __uuid
from app.services.chat_image_service import build_history_with_images


async def test_get_conversation_messages_excludes_inactive_sibling(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    """An inactive sibling (a superseded regenerate/edit version) and a
    soft-deleted node must never enter model context — only the active branch."""
    conv_id = conversation.id
    base = datetime.now(timezone.utc)

    # Active turn 1: user -> assistant(active version B). Version A is the
    # deactivated sibling that must be excluded.
    db_session.add(Message(
        conversation_id=conv_id, role="user", content="вопрос",
        is_active=True, created_at=base,
    ))
    db_session.add(Message(
        conversation_id=conv_id, role="assistant", content="старый ответ A",
        model_id=MODEL_ID, input_tokens=1, output_tokens=1, cost_micro_rub=1,
        request_id=__uuid.uuid4(), is_active=False,  # superseded sibling
        created_at=base + timedelta(milliseconds=10),
    ))
    db_session.add(Message(
        conversation_id=conv_id, role="assistant", content="новый ответ B",
        model_id=MODEL_ID, input_tokens=1, output_tokens=1, cost_micro_rub=1,
        request_id=__uuid.uuid4(), is_active=True,  # active version
        created_at=base + timedelta(milliseconds=20),
    ))
    # A soft-deleted abandoned node — excluded even though is_active defaults true.
    db_session.add(Message(
        conversation_id=conv_id, role="user", content="брошенный узел",
        is_active=True, deleted_at=base + timedelta(milliseconds=30),
        created_at=base + timedelta(milliseconds=25),
    ))
    await db_session.commit()

    result = await get_conversation_messages(db_session, conv_id)
    assert result == [
        {"role": "user", "content": "вопрос"},
        {"role": "assistant", "content": "новый ответ B"},
    ]
    assert {"role": "assistant", "content": "старый ответ A"} not in result
    assert all(m["content"] != "брошенный узел" for m in result)


async def test_build_history_with_images_excludes_inactive_sibling(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    """The multimodal/cost-estimation history pull is filtered identically."""
    conv_id = conversation.id
    base = datetime.now(timezone.utc)
    db_session.add(Message(
        conversation_id=conv_id, role="user", content="вопрос",
        is_active=True, created_at=base,
    ))
    db_session.add(Message(
        conversation_id=conv_id, role="assistant", content="старый ответ A",
        model_id=MODEL_ID, input_tokens=1, output_tokens=1, cost_micro_rub=1,
        request_id=__uuid.uuid4(), is_active=False,
        created_at=base + timedelta(milliseconds=10),
    ))
    db_session.add(Message(
        conversation_id=conv_id, role="assistant", content="новый ответ B",
        model_id=MODEL_ID, input_tokens=1, output_tokens=1, cost_micro_rub=1,
        request_id=__uuid.uuid4(), is_active=True,
        created_at=base + timedelta(milliseconds=20),
    ))
    await db_session.commit()

    history = await build_history_with_images(db_session, conv_id)
    assert history == [
        {"role": "user", "content": "вопрос"},
        {"role": "assistant", "content": "новый ответ B"},
    ]
