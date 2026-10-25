"""tr-9wq: denormalized branch totals + per-user artifact bytes (DB triggers)."""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.artifact import Artifact
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def test_schema_objects_exist(db_session: AsyncSession):
    cols = (await db_session.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='conversations' AND column_name LIKE 'bt_%'"
    ))).scalars().all()
    assert set(cols) == {
        "bt_total_cost_micro_rub", "bt_total_input_tokens", "bt_total_output_tokens",
        "bt_total_reasoning_tokens", "bt_message_count", "bt_generation_count",
    }
    ucol = (await db_session.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='users' AND column_name='artifact_bytes_used'"
    ))).first()
    assert ucol is not None
    idx = (await db_session.execute(text(
        "SELECT 1 FROM pg_indexes WHERE indexname='idx_artifacts_conv_filename'"
    ))).first()
    assert idx is not None
    trg = (await db_session.execute(text(
        "SELECT count(*) FROM pg_trigger WHERE tgname IN "
        "('messages_branch_totals','artifacts_user_bytes')"
    ))).scalar_one()
    assert trg == 2


@pytest_asyncio.fixture
async def u(db_session: AsyncSession) -> User:
    user = User(email=f"den-{uuid.uuid4().hex[:8]}@x.io", display_name="D",
                password_hash=None, role="user", status="active",
                balance_micro_rub=10_000_000, reserved_micro_rub=0)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def conv(db_session: AsyncSession, u: User) -> Conversation:
    c = Conversation(user_id=u.id, model_id="m", title="t")
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


# The DB triggers update conversations.bt_* / users.artifact_bytes_used behind
# SQLAlchemy's back, so the in-session ORM rows are stale. We must re-read with
# `populate_existing()` (overwrites the identity-map row) rather than
# `expire_all()` + lazy access: expiring an ORM attr and then touching it in an
# async session fires a SYNC lazy-load outside the greenlet -> MissingGreenlet.
# Test code therefore captures bare UUIDs up front and never re-reads an ORM attr.
async def _bt(db_session, conv_id):
    c = (await db_session.execute(
        select(Conversation).where(Conversation.id == conv_id).execution_options(populate_existing=True)
    )).scalar_one()
    return (c.bt_total_cost_micro_rub, c.bt_total_input_tokens, c.bt_total_output_tokens,
            c.bt_total_reasoning_tokens, c.bt_message_count, c.bt_generation_count)


async def test_messages_trigger_insert_and_softdelete(db_session: AsyncSession, conv: Conversation):
    conv_id = conv.id
    assert await _bt(db_session, conv_id) == (0, 0, 0, 0, 0, 0)
    m = Message(conversation_id=conv_id, role="assistant", content="hi",
                cost_micro_rub=500, input_tokens=10, output_tokens=20, reasoning_tokens=5)
    db_session.add(m); await db_session.commit()
    m_id = m.id
    assert await _bt(db_session, conv_id) == (500, 10, 20, 5, 1, 1)
    um = Message(conversation_id=conv_id, role="user", content="q")
    db_session.add(um); await db_session.commit()
    um_id = um.id
    assert await _bt(db_session, conv_id) == (500, 10, 20, 5, 2, 1)
    await db_session.execute(update(Message).where(Message.id == m_id).values(deleted_at=text("now()")))
    await db_session.commit()
    assert await _bt(db_session, conv_id) == (0, 0, 0, 0, 1, 0)
    await db_session.execute(text("DELETE FROM messages WHERE id = :i").bindparams(i=um_id))
    await db_session.commit()
    assert await _bt(db_session, conv_id) == (0, 0, 0, 0, 0, 0)


async def test_messages_trigger_noop_update_does_not_change_totals(db_session: AsyncSession, conv: Conversation):
    conv_id = conv.id
    m = Message(conversation_id=conv_id, role="assistant", content="hi",
                cost_micro_rub=300, input_tokens=3, output_tokens=4, reasoning_tokens=0)
    db_session.add(m); await db_session.commit()
    m_id = m.id
    before = await _bt(db_session, conv_id)
    await db_session.execute(update(Message).where(Message.id == m_id).values(is_active=False))
    await db_session.commit()
    assert await _bt(db_session, conv_id) == before


async def test_artifacts_trigger_user_bytes(db_session: AsyncSession, u: User, conv: Conversation):
    u_id, conv_id = u.id, conv.id

    async def bytes_used():
        return (await db_session.execute(
            select(User.artifact_bytes_used).where(User.id == u_id)
        )).scalar_one()
    assert await bytes_used() == 0
    a1 = Artifact(user_id=u_id, conversation_id=conv_id, group_id=uuid.uuid4(), version=1,
                  filename="a.md", media_type="text/markdown", content="x", size_bytes=100)
    db_session.add(a1); await db_session.commit()
    a1_id = a1.id
    assert await bytes_used() == 100
    a2 = Artifact(user_id=u_id, conversation_id=conv_id, group_id=uuid.uuid4(), version=1,
                  filename="b.md", media_type="text/markdown", content="yy", size_bytes=250)
    db_session.add(a2); await db_session.commit()
    assert await bytes_used() == 350
    await db_session.execute(text("DELETE FROM artifacts WHERE id = :i").bindparams(i=a1_id))
    await db_session.commit()
    assert await bytes_used() == 250


async def test_permanent_delete_conversation_decrements_user_bytes(db_session: AsyncSession, u: User, conv: Conversation):
    # Precondition: artifacts.conversation_id must CASCADE on conversation delete
    # (declared by migration 017) so the deletion reaches the artifacts_user_bytes
    # trigger. 'c' = CASCADE in pg_constraint.confdeltype; 'a'/'r'/'n'/etc. would
    # block or orphan the row, never decrementing the quota. Asserted explicitly so
    # a drifted FK fails here with a clear message instead of an opaque 400 != 0.
    confdeltype = (await db_session.execute(text(
        "SELECT confdeltype FROM pg_constraint "
        "WHERE conname = 'artifacts_conversation_id_fkey'"
    ))).scalar_one()
    # pg_constraint.confdeltype is the "char" type → asyncpg returns it as bytes.
    ondelete = confdeltype.decode() if isinstance(confdeltype, (bytes, bytearray)) else confdeltype
    assert ondelete == "c", (
        "artifacts_conversation_id_fkey is not ON DELETE CASCADE "
        f"(confdeltype={confdeltype!r}); migrations 017/019 declare CASCADE — this DB "
        "has a drifted FK and permanent-delete will not release per-user bytes."
    )
    u_id, conv_id = u.id, conv.id
    a = Artifact(user_id=u_id, conversation_id=conv_id, group_id=uuid.uuid4(), version=1,
                 filename="c.md", media_type="text/markdown", content="zzz", size_bytes=400)
    db_session.add(a); await db_session.commit()
    assert (await db_session.execute(select(User.artifact_bytes_used).where(User.id == u_id))).scalar_one() == 400
    await db_session.execute(text("DELETE FROM conversations WHERE id = :i").bindparams(i=conv_id))
    await db_session.commit()
    assert (await db_session.execute(select(User.artifact_bytes_used).where(User.id == u_id))).scalar_one() == 0


async def test_backfill_corrects_columns_from_raw_data(db_session: AsyncSession, u: User, conv: Conversation):
    # Insert rows (triggers set counters), then corrupt the columns with a direct
    # UPDATE (which does NOT fire the messages/artifacts triggers), then run the
    # migration's backfill SQL and assert the counters are recomputed correctly.
    conv_id, u_id = conv.id, u.id
    db_session.add(Message(conversation_id=conv_id, role="assistant", content="a",
                           cost_micro_rub=700, input_tokens=11, output_tokens=22, reasoning_tokens=3))
    db_session.add(Artifact(user_id=u_id, conversation_id=conv_id, group_id=uuid.uuid4(), version=1,
                            filename="d.md", media_type="text/markdown", content="x", size_bytes=900))
    await db_session.commit()
    await db_session.execute(update(Conversation).where(Conversation.id == conv_id).values(
        bt_total_cost_micro_rub=-1, bt_message_count=-1, bt_generation_count=-1))
    await db_session.execute(update(User).where(User.id == u_id).values(artifact_bytes_used=-1))
    await db_session.commit()
    await db_session.execute(text("""
        UPDATE conversations c SET
            bt_total_cost_micro_rub=agg.cost, bt_total_input_tokens=agg.in_tok,
            bt_total_output_tokens=agg.out_tok, bt_total_reasoning_tokens=agg.reason_tok,
            bt_message_count=agg.msg_count, bt_generation_count=agg.gen_count
        FROM (SELECT conversation_id, COALESCE(SUM(cost_micro_rub),0) cost,
              COALESCE(SUM(input_tokens),0) in_tok, COALESCE(SUM(output_tokens),0) out_tok,
              COALESCE(SUM(reasoning_tokens),0) reason_tok, COUNT(*) msg_count,
              COUNT(*) FILTER (WHERE role='assistant') gen_count
              FROM messages WHERE deleted_at IS NULL GROUP BY conversation_id) agg
        WHERE c.id=agg.conversation_id"""))
    await db_session.execute(text(
        "UPDATE users u SET artifact_bytes_used = COALESCE((SELECT SUM(size_bytes) "
        "FROM artifacts a WHERE a.user_id=u.id),0)"))
    await db_session.commit()
    assert await _bt(db_session, conv_id) == (700, 11, 22, 3, 1, 1)
    assert (await db_session.execute(select(User.artifact_bytes_used).where(User.id == u_id))).scalar_one() == 900
