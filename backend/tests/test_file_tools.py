"""File-tool executors: create/edit, diff, quotas, error paths (tr-cy0.2.3).

Data-only over the artifacts table; executors never raise on bad input — they
return ToolResultPayload(status="error", …) so the model can adapt.
"""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.artifact import Artifact
from app.models.conversation import Conversation
from app.models.user import User
from app.services.tools import file_tools, registry
from app.services.tools.file_tools import (
    ToolContext,
    create_file,
    str_replace,
    view_file,
)

pytestmark = pytest.mark.asyncio


async def test_view_file_returns_current_content(db_session, user, conversation):
    # tr-7ml: a cross-turn edit needs to SEE the file first. view_file returns the
    # latest content and writes nothing.
    await create_file(
        {"path": "notes.md", "file_text": "line1\nline2"},
        _ctx(db_session, user, conversation),
    )
    ctx = _ctx(db_session, user, conversation)
    res = await view_file({"path": "notes.md"}, ctx)
    assert res.status == "ok"
    assert res.filename == "notes.md"
    assert "line1\nline2" in res.output
    assert ctx.created_artifact_ids == []  # a read creates no version


async def test_view_file_returns_latest_version(db_session, user, conversation):
    c = _ctx(db_session, user, conversation)
    await create_file({"path": "doc.md", "file_text": "старый текст"}, c)
    await str_replace(
        {"path": "doc.md", "old_str": "старый", "new_str": "новый"}, c
    )
    res = await view_file({"path": "doc.md"}, _ctx(db_session, user, conversation))
    assert res.status == "ok"
    assert "новый текст" in res.output
    assert "старый" not in res.output


async def test_view_file_missing_returns_error(db_session, user, conversation):
    res = await view_file(
        {"path": "ghost.md"}, _ctx(db_session, user, conversation)
    )
    assert res.status == "error"
    assert "ghost.md" in res.output


async def test_view_file_registered_in_definitions():
    file_tools.register_builtin_tools()
    names = {d["function"]["name"] for d in registry.definitions()}
    assert "view_file" in names


async def test_str_replace_not_found_hints_view_file(db_session, user, conversation):
    c = _ctx(db_session, user, conversation)
    await create_file({"path": "f.md", "file_text": "содержимое"}, c)
    res = await str_replace(
        {"path": "f.md", "old_str": "ОТСУТСТВУЕТ", "new_str": "x"},
        _ctx(db_session, user, conversation),
    )
    assert res.status == "error"
    # The model is nudged toward view_file so it stops guessing the content.
    assert "view_file" in res.output


@pytest_asyncio.fixture
async def user(db_session: AsyncSession) -> User:
    u = User(
        email="filetools_test@example.com",
        display_name="ФТ",
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


def _ctx(db, user, conv) -> ToolContext:
    return ToolContext(db=db, user_id=user.id, conversation_id=conv.id)


async def test_create_file_persists_artifact(db_session, user, conversation):
    ctx = _ctx(db_session, user, conversation)
    res = await create_file(
        {"path": "python_exam_prep.md", "file_text": "line1\nline2\nline3"}, ctx
    )
    assert res.status == "ok"
    assert res.filename == "python_exam_prep.md"
    assert res.diff.added == 3 and res.diff.removed == 0
    # presented is NOT a per-step flag (D7) — the artifact card presents the file.
    assert res.presented is False
    assert len(ctx.created_artifact_ids) == 1

    row = (
        await db_session.execute(
            select(Artifact).where(Artifact.filename == "python_exam_prep.md")
        )
    ).scalar_one()
    assert row.version == 1 and row.content == "line1\nline2\nline3"


async def test_str_replace_creates_new_version_with_diff(db_session, user, conversation):
    ctx = _ctx(db_session, user, conversation)
    await create_file({"path": "a.md", "file_text": "alpha\nbeta\ngamma"}, ctx)
    res = await str_replace(
        {"path": "a.md", "old_str": "beta", "new_str": "BETA"}, ctx
    )
    assert res.status == "ok"
    assert res.diff.added == 1 and res.diff.removed == 1
    latest = (
        await db_session.execute(
            select(Artifact).where(Artifact.filename == "a.md").order_by(Artifact.version.desc())
        )
    ).scalars().first()
    assert latest.version == 2 and "BETA" in latest.content


async def test_str_replace_not_found(db_session, user, conversation):
    ctx = _ctx(db_session, user, conversation)
    res = await str_replace({"path": "missing.md", "old_str": "x", "new_str": "y"}, ctx)
    assert res.status == "error" and "не найден" in res.output


async def test_str_replace_ambiguous(db_session, user, conversation):
    ctx = _ctx(db_session, user, conversation)
    await create_file({"path": "b.md", "file_text": "dup\ndup"}, ctx)
    res = await str_replace({"path": "b.md", "old_str": "dup", "new_str": "z"}, ctx)
    assert res.status == "error" and "неоднозначен" in res.output


async def test_create_file_rejects_bad_path(db_session, user, conversation):
    ctx = _ctx(db_session, user, conversation)
    res = await create_file({"path": "../escape", "file_text": "x"}, ctx)
    assert res.status == "error"
    # nothing persisted
    rows = (await db_session.execute(select(Artifact))).scalars().all()
    assert rows == []


async def test_per_conversation_count_quota(
    db_session, user, conversation, monkeypatch
):
    monkeypatch.setattr(file_tools, "MAX_ARTIFACTS_PER_CONVERSATION", 2)
    ctx = _ctx(db_session, user, conversation)
    assert (await create_file({"path": "f1.md", "file_text": "a"}, ctx)).status == "ok"
    assert (await create_file({"path": "f2.md", "file_text": "b"}, ctx)).status == "ok"
    third = await create_file({"path": "f3.md", "file_text": "c"}, ctx)
    assert third.status == "error" and "лимит файлов" in third.output


async def test_per_user_bytes_quota(db_session, user, conversation, monkeypatch):
    monkeypatch.setattr(file_tools, "MAX_ARTIFACT_BYTES_PER_USER", 10)
    ctx = _ctx(db_session, user, conversation)
    ok = await create_file({"path": "small.md", "file_text": "12345"}, ctx)
    assert ok.status == "ok"
    too_big = await create_file({"path": "big.md", "file_text": "1234567890X"}, ctx)
    assert too_big.status == "error" and "лимит хранилища" in too_big.output


async def test_create_file_errors_on_existing_path(db_session, user, conversation):
    # create_file never silently overwrites (D1b) — existing path is an error.
    ctx = _ctx(db_session, user, conversation)
    await create_file({"path": "o.md", "file_text": "v1"}, ctx)
    res = await create_file({"path": "o.md", "file_text": "v2"}, ctx)
    assert res.status == "error" and "уже существует" in res.output
    rows = (
        await db_session.execute(select(Artifact).where(Artifact.filename == "o.md"))
    ).scalars().all()
    assert len(rows) == 1 and rows[0].content == "v1"  # untouched


async def test_str_replace_caps_versions(db_session, user, conversation, monkeypatch):
    monkeypatch.setattr(file_tools, "MAX_VERSIONS_PER_GROUP", 3)
    ctx = _ctx(db_session, user, conversation)
    await create_file({"path": "c.md", "file_text": "v0"}, ctx)
    for i in range(5):
        r = await str_replace({"path": "c.md", "old_str": f"v{i}", "new_str": f"v{i + 1}"}, ctx)
        assert r.status == "ok"
    rows = (
        await db_session.execute(
            select(Artifact).where(Artifact.filename == "c.md").order_by(Artifact.version)
        )
    ).scalars().all()
    assert len(rows) == 3  # capped
    assert rows[-1].version == 6 and rows[-1].content == "v5"


async def test_str_replace_quota_counts_all_versions(
    db_session, user, conversation, monkeypatch
):
    # All version rows count toward the per-user quota (the version bug fix).
    monkeypatch.setattr(file_tools, "MAX_ARTIFACT_BYTES_PER_USER", 30)
    monkeypatch.setattr(file_tools, "MAX_VERSIONS_PER_GROUP", 100)  # no pruning
    ctx = _ctx(db_session, user, conversation)
    await create_file({"path": "q.md", "file_text": "1234567890"}, ctx)  # 10 bytes v1
    assert (await str_replace({"path": "q.md", "old_str": "1234567890", "new_str": "abcdefghij"}, ctx)).status == "ok"  # v2 → 20
    assert (await str_replace({"path": "q.md", "old_str": "abcdefghij", "new_str": "ABCDEFGHIJ"}, ctx)).status == "ok"  # v3 → 30
    over = await str_replace({"path": "q.md", "old_str": "ABCDEFGHIJ", "new_str": "klmnopqrst"}, ctx)  # → 40 > 30
    assert over.status == "error" and "лимит хранилища" in over.output
