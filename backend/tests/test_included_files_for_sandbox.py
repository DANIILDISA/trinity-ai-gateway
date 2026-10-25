"""included_files_for_sandbox: the conversation's included chat_files as sandbox seed
inputs for the run_python UPLOAD path (tr-an0g.4). Included files only, ordered by
created_at, deduped by basename (latest upload of a name wins).
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_file import ChatFile
from app.models.conversation import Conversation
from app.models.user import User
from app.services.chat_file_service import included_files_for_sandbox

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def user(db_session: AsyncSession) -> User:
    u = User(
        email="seed_fetch@example.com", display_name="U", password_hash=None,
        role="user", status="active", balance_micro_rub=1_000_000, reserved_micro_rub=0,
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


def _cf(user, conv, filename, data_b64, *, included=True, created=None, media="text/csv"):
    return ChatFile(
        user_id=user.id, conversation_id=conv.id, message_id=None,
        filename=filename, media_type=media, kind="text", base64_data=data_b64,
        file_size_bytes=10, token_estimate=1, included_in_context=included,
        **({"created_at": created} if created else {}),
    )


async def test_returns_included_files_only(db_session, user, conversation):
    db_session.add_all([
        _cf(user, conversation, "keep.csv", "a2VlcA=="),
        _cf(user, conversation, "drop.csv", "ZHJvcA==", included=False),
    ])
    await db_session.commit()
    out = await included_files_for_sandbox(db_session, conversation.id)
    assert [f["filename"] for f in out] == ["keep.csv"]
    assert out[0]["base64"] == "a2VlcA=="
    assert out[0]["media_type"] == "text/csv"


async def test_dedups_basename_latest_wins(db_session, user, conversation):
    base = datetime(2026, 6, 30, tzinfo=timezone.utc)
    db_session.add_all([
        _cf(user, conversation, "data.csv", "b2xk", created=base),                    # old
        _cf(user, conversation, "data.csv", "bmV3", created=base + timedelta(hours=1)),  # new
    ])
    await db_session.commit()
    out = await included_files_for_sandbox(db_session, conversation.id)
    assert len(out) == 1
    assert out[0]["filename"] == "data.csv"
    assert out[0]["base64"] == "bmV3"  # latest upload of the name wins


async def test_empty_when_no_included_files(db_session, user, conversation):
    db_session.add(_cf(user, conversation, "x.csv", "eA==", included=False))
    await db_session.commit()
    assert await included_files_for_sandbox(db_session, conversation.id) == []
