"""DB foundation for agentic tool-use (tr-cy0.2.1 / tr-cy0.1.10).

The versioned `artifacts` table (model-authored files) and the `messages.tool_steps`
JSONB column (the live tool-step timeline, persisted for reload/post-Stop) must
round-trip through the ORM.
"""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.artifact import Artifact
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def user(db_session: AsyncSession) -> User:
    u = User(
        email="artifact_test@example.com",
        display_name="Артефакт Тест",
        password_hash=None,
        role="user",
        status="active",
        balance_micro_rub=1_000_000,
        reserved_micro_rub=0,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


@pytest_asyncio.fixture
async def conversation(db_session: AsyncSession, user: User) -> Conversation:
    conv = Conversation(user_id=user.id, model_id="anthropic/claude-haiku-4-5", title="A")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    return conv


async def test_artifact_round_trips(
    db_session: AsyncSession, user: User, conversation: Conversation
):
    group = uuid.uuid4()
    content = "# Python exam prep\n\nВопрос 1...\n"
    art = Artifact(
        user_id=user.id,
        conversation_id=conversation.id,
        message_id=None,
        group_id=group,
        version=1,
        filename="python_exam_prep.md",
        media_type="text/markdown",
        content=content,
        size_bytes=len(content.encode("utf-8")),
    )
    db_session.add(art)
    await db_session.commit()
    db_session.expunge_all()

    row = (
        await db_session.execute(
            select(Artifact).where(Artifact.group_id == group)
        )
    ).scalar_one()
    assert row.filename == "python_exam_prep.md"
    assert row.version == 1
    assert row.content == content
    assert row.size_bytes == len(content.encode("utf-8"))
    assert row.media_type == "text/markdown"


async def test_message_tool_steps_round_trips(
    db_session: AsyncSession, conversation: Conversation
):
    steps = [
        {
            "id": "tc-0-0",
            "tool": "create_file",
            "title": "Создание файла python_exam_prep.md",
            "filename": "python_exam_prep.md",
            "diff": {"added": 42, "removed": 0},
            "presented": True,
            "status": "done",
        }
    ]
    msg = Message(
        conversation_id=conversation.id,
        role="assistant",
        content="Готово",
        tool_steps=steps,
    )
    db_session.add(msg)
    await db_session.commit()
    db_session.expunge_all()

    row = (
        await db_session.execute(
            select(Message).where(Message.conversation_id == conversation.id)
        )
    ).scalar_one()
    assert row.tool_steps == steps


async def test_message_tool_steps_defaults_null(
    db_session: AsyncSession, conversation: Conversation
):
    msg = Message(conversation_id=conversation.id, role="assistant", content="hi")
    db_session.add(msg)
    await db_session.commit()
    db_session.expunge_all()
    row = (
        await db_session.execute(
            select(Message).where(Message.conversation_id == conversation.id)
        )
    ).scalar_one()
    assert row.tool_steps is None
