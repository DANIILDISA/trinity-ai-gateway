"""included_files_seed_hash / _with_hash: a cheap metadata-only hash (no base64) over the
conversation's included chat_files winning the basename dedup. Changes iff the seeded set
changes; agrees with included_files_for_sandbox on which rows win. chat_files rows are
insert-only => row id == content identity (tr-f63e B3 two-phase seed protocol).
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_file import ChatFile
from app.models.conversation import Conversation
from app.models.user import User
from app.services.chat_file_service import (
    included_files_for_sandbox,
    included_files_for_sandbox_with_hash,
    included_files_seed_hash,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def user(db_session: AsyncSession) -> User:
    u = User(
        email="seed_hash@example.com", display_name="U", password_hash=None,
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


async def test_hash_none_when_no_included_files(db_session, user, conversation):
    db_session.add(_cf(user, conversation, "x.csv", "eA==", included=False))
    await db_session.commit()
    assert await included_files_seed_hash(db_session, conversation.id) is None


async def test_hash_deterministic_and_stable_on_refetch(db_session, user, conversation):
    db_session.add(_cf(user, conversation, "a.csv", "YQ=="))
    await db_session.commit()
    h1 = await included_files_seed_hash(db_session, conversation.id)
    h2 = await included_files_seed_hash(db_session, conversation.id)
    assert h1 is not None and h1 == h2


async def test_hash_changes_when_newer_same_basename_added(db_session, user, conversation):
    base = datetime(2026, 6, 30, tzinfo=timezone.utc)
    db_session.add(_cf(user, conversation, "data.csv", "b2xk", created=base))
    await db_session.commit()
    before = await included_files_seed_hash(db_session, conversation.id)
    db_session.add(_cf(user, conversation, "data.csv", "bmV3", created=base + timedelta(hours=1)))
    await db_session.commit()
    after = await included_files_seed_hash(db_session, conversation.id)
    assert before != after


async def test_hash_changes_when_file_excluded(db_session, user, conversation):
    cf = _cf(user, conversation, "keep.csv", "a2VlcA==")
    db_session.add(cf)
    await db_session.commit()
    before = await included_files_seed_hash(db_session, conversation.id)
    cf.included_in_context = False
    await db_session.commit()
    after = await included_files_seed_hash(db_session, conversation.id)
    assert before is not None and after is None


async def test_hash_winning_set_agrees_with_full_fetch(db_session, user, conversation):
    base = datetime(2026, 6, 30, tzinfo=timezone.utc)
    db_session.add_all([
        _cf(user, conversation, "data.csv", "b2xk", created=base),
        _cf(user, conversation, "data.csv", "bmV3", created=base + timedelta(hours=1)),
        _cf(user, conversation, "other.csv", "eA=="),
        _cf(user, conversation, "drop.csv", "ZA==", included=False),
    ])
    await db_session.commit()
    h = await included_files_seed_hash(db_session, conversation.id)
    full = await included_files_for_sandbox(db_session, conversation.id)
    assert sorted(f["filename"] for f in full) == ["data.csv", "other.csv"]
    assert h is not None


async def test_with_hash_returns_files_and_matching_hash(db_session, user, conversation):
    db_session.add(_cf(user, conversation, "a.csv", "YQ=="))
    await db_session.commit()
    files, h = await included_files_for_sandbox_with_hash(db_session, conversation.id)
    assert [f["filename"] for f in files] == ["a.csv"]
    assert files[0]["base64"] == "YQ==" and files[0]["media_type"] == "text/csv"
    assert h == await included_files_seed_hash(db_session, conversation.id)
