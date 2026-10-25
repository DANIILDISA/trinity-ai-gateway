"""Tests for branching_service (Task 3.5 — message version history)."""
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.services.branching_service import (
    deactivate_active_tail_from,
    derive_version_info,
    heal_orphaned_active_path,
    lock_conversation,
    repoint_active_leaf,
    resolve_active_path,
    restore_orphaned_answer,
    set_active_leaf_to,
)

TEST_EMAIL = "branching_service@example.com"
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
        email=TEST_EMAIL, display_name="Ветвление Тест", password_hash=None,
        role="user", status="active",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    conv = Conversation(user_id=user.id, model_id=MODEL_ID, title="Ветка")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    yield conv
    db_session.expire_all()
    await _purge(db_session)


def _mk(conv_id, role, content, *, ms, parent_id=None, is_active=True, deleted_at=None):
    base = datetime(2026, 6, 8, tzinfo=timezone.utc)
    return Message(
        conversation_id=conv_id, role=role, content=content,
        parent_id=parent_id, is_active=is_active, deleted_at=deleted_at,
        model_id=MODEL_ID if role == "assistant" else None,
        request_id=_uuid.uuid4() if role == "assistant" else None,
        created_at=base + timedelta(milliseconds=ms),
    )


async def test_lock_conversation_returns_row(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    locked = await lock_conversation(db_session, conversation.id)
    assert locked.id == conversation.id


async def test_resolve_active_path_uses_leaf_and_excludes_inactive(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    conv_id = conversation.id
    u = _mk(conv_id, "user", "вопрос", ms=0)
    db_session.add(u)
    await db_session.flush()
    a_old = _mk(conv_id, "assistant", "A", ms=10, parent_id=u.id, is_active=False)
    a_new = _mk(conv_id, "assistant", "B", ms=20, parent_id=u.id, is_active=True)
    db_session.add_all([a_old, a_new])
    await db_session.flush()
    conversation.active_leaf_id = a_new.id
    await db_session.commit()

    path = await resolve_active_path(db_session, conv_id)
    assert [m.content for m in path] == ["вопрос", "B"]


async def test_resolve_active_path_legacy_fallback(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    """NULL active_leaf_id (pre-014 row) -> created_at ordering, active rows only."""
    conv_id = conversation.id
    db_session.add_all([
        _mk(conv_id, "user", "первое", ms=0),
        _mk(conv_id, "assistant", "ответ", ms=10),
        _mk(conv_id, "user", "второе", ms=20),
    ])
    await db_session.commit()
    assert conversation.active_leaf_id is None

    path = await resolve_active_path(db_session, conv_id)
    assert [m.content for m in path] == ["первое", "ответ", "второе"]


async def test_deactivate_active_tail_from(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    conv_id = conversation.id
    u = _mk(conv_id, "user", "вопрос", ms=0)
    db_session.add(u)
    await db_session.flush()
    a = _mk(conv_id, "assistant", "ответ", ms=10, parent_id=u.id)
    db_session.add(a)
    await db_session.flush()
    u2 = _mk(conv_id, "user", "ещё", ms=20, parent_id=a.id)
    db_session.add(u2)
    await db_session.flush()
    conversation.active_leaf_id = u2.id
    await db_session.commit()

    # Deactivate from the assistant down: a and u2 go inactive, the first user stays.
    await deactivate_active_tail_from(db_session, conv_id, a)
    await db_session.commit()
    db_session.expire_all()

    rows = {
        m.content: m.is_active
        for m in (
            await db_session.execute(
                select(Message).where(Message.conversation_id == conv_id)
            )
        ).scalars()
    }
    assert rows == {"вопрос": True, "ответ": False, "ещё": False}


async def test_deactivate_active_tail_from_tiebreaks_on_id(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    """Two active siblings with IDENTICAL created_at (co-created in one txn, as the
    edit/regenerate endpoints do): deactivating from the LATER one (by id) must
    leave the EARLIER one active. A plain `created_at >= ...` boundary would
    over-deactivate the earlier sibling too (design §5 tie-break)."""
    conv_id = conversation.id
    # Same created_at (ms=0) for both; the (created_at, id) tuple disambiguates.
    a = _mk(conv_id, "assistant", "A", ms=0)
    b = _mk(conv_id, "assistant", "B", ms=0)
    db_session.add_all([a, b])
    await db_session.flush()
    # Pick the lexicographically-LATER row by (created_at, id) as the boundary.
    earlier, later = sorted([a, b], key=lambda m: (m.created_at, m.id))
    earlier_id, later_id = earlier.id, later.id
    await db_session.commit()

    await deactivate_active_tail_from(db_session, conv_id, later)
    await db_session.commit()
    db_session.expire_all()

    states = {
        m.id: m.is_active
        for m in (
            await db_session.execute(
                select(Message).where(Message.conversation_id == conv_id)
            )
        ).scalars()
    }
    # The boundary row goes inactive; its same-created_at, lower-id sibling stays.
    assert states[later_id] is False
    assert states[earlier_id] is True


async def test_repoint_active_leaf(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    conv_id = conversation.id
    u = _mk(conv_id, "user", "вопрос", ms=0)
    db_session.add(u)
    await db_session.flush()
    # Capture the id before expire_all(): touching an expired ORM attribute
    # would trigger a synchronous lazy reload (MissingGreenlet under asyncpg).
    u_id = u.id
    await repoint_active_leaf(db_session, conversation, u_id)
    await db_session.commit()
    db_session.expire_all()
    refreshed = (
        await db_session.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    assert refreshed == u_id


async def test_derive_version_info_groups_siblings(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    conv_id = conversation.id
    u = _mk(conv_id, "user", "вопрос", ms=0)
    db_session.add(u)
    await db_session.flush()
    a1 = _mk(conv_id, "assistant", "A", ms=10, parent_id=u.id, is_active=False)
    a2 = _mk(conv_id, "assistant", "B", ms=20, parent_id=u.id, is_active=True)
    db_session.add_all([a1, a2])
    await db_session.commit()

    info = await derive_version_info(db_session, conv_id)
    # The lone user message: single version.
    assert info[u.id] == (1, 1)
    # Two assistant siblings ordered by (created_at, id): A=1/2, B=2/2.
    assert info[a1.id] == (1, 2)
    assert info[a2.id] == (2, 2)


async def _active_chain(db: AsyncSession, conv_id) -> list[Message]:
    """The active, non-deleted rows ordered root→leaf (created_at, id)."""
    return list(
        (
            await db.execute(
                select(Message)
                .where(
                    Message.conversation_id == conv_id,
                    Message.is_active.is_(True),
                    Message.deleted_at.is_(None),
                )
                .order_by(Message.created_at.asc(), Message.id.asc())
            )
        ).scalars()
    )


def _assert_single_contiguous_path(rows: list[Message], leaf_id, active_leaf_id) -> None:
    """Exactly one active leaf == active_leaf_id, the active set is one contiguous
    root→leaf chain, and no parent has two active children."""
    active_ids = {m.id for m in rows}
    parent_ids = {m.parent_id for m in rows if m.parent_id is not None}
    leaves = active_ids - parent_ids
    assert leaves == {leaf_id}, f"expected single active leaf {leaf_id}, got {leaves}"
    assert active_leaf_id == leaf_id
    by_parent: dict = {}
    for m in rows:
        assert m.parent_id not in by_parent, "two active siblings share a parent"
        by_parent[m.parent_id] = m
    node = by_parent.get(None)
    assert node is not None, "no active root"
    walked = 1
    while node.id in parent_ids:
        node = by_parent[node.id]
        walked += 1
    assert node.id == leaf_id, "walk did not end at leaf"
    assert walked == len(rows), "active set is not a single contiguous chain"


async def test_set_active_leaf_to_heals_multi_active(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    """Directly seed a corrupt two-active-leaf state (two assistant siblings BOTH
    is_active=true under one user) and prove set_active_leaf_to collapses it to a
    single contiguous active path ending at the chosen leaf — the §8 self-heal."""
    conv_id = conversation.id
    u = _mk(conv_id, "user", "вопрос", ms=0)
    db_session.add(u)
    await db_session.flush()
    a_other = _mk(conv_id, "assistant", "A", ms=10, parent_id=u.id, is_active=True)
    a_chosen = _mk(conv_id, "assistant", "B", ms=20, parent_id=u.id, is_active=True)
    db_session.add_all([a_other, a_chosen])
    await db_session.flush()
    u_id, a_other_id, a_chosen_id = u.id, a_other.id, a_chosen.id
    # Messy denormalized pointer: points at the *other* leaf.
    conversation.active_leaf_id = a_other_id
    await db_session.commit()

    # Sanity: the seeded state really has TWO active leaves.
    db_session.expire_all()
    pre = await _active_chain(db_session, conv_id)
    pre_leaves = {m.id for m in pre} - {m.parent_id for m in pre if m.parent_id}
    assert pre_leaves == {a_other_id, a_chosen_id}

    conv = await lock_conversation(db_session, conv_id)
    await set_active_leaf_to(db_session, conv, a_chosen_id)
    await db_session.commit()

    db_session.expire_all()
    rows = await _active_chain(db_session, conv_id)
    leaf = (
        await db_session.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    _assert_single_contiguous_path(rows, a_chosen_id, leaf)
    assert [m.id for m in rows] == [u_id, a_chosen_id]
    # The other branch is set aside, never deleted.
    a_other_row = (
        await db_session.execute(select(Message).where(Message.id == a_other_id))
    ).scalar_one()
    assert a_other_row.is_active is False
    assert a_other_row.deleted_at is None


async def test_set_active_leaf_to_last_writer_wins_under_concurrent_wiring(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    """BUG-2 sequential proxy for the edit↔regenerate wiring race.

    Simulate both branch mutations landing in one transaction (the lock is
    released before streaming, so two in-flight assistants can co-exist), then run
    BOTH wirings in sequence the way two racing requests would under the
    conversation lock — last writer wins. set_active_leaf_to must leave EXACTLY
    one active leaf with a coherent path, and never chain two assistants without a
    user between them.

    Tree:
        U1 ─ A1                       (original)
        U1 ─ A1r                      (regenerate: sibling of A1 under U1)
        U1e ─ A1e                     (edit: new user sibling U1e + its assistant)
    Edit wires A1e (off U1e); regenerate wires A1r (off U1). Whichever writes last
    owns the single active leaf; the loser's branch is fully deactivated.
    """
    conv_id = conversation.id
    u1 = _mk(conv_id, "user", "вопрос", ms=0)
    db_session.add(u1)
    await db_session.flush()
    a1 = _mk(conv_id, "assistant", "A1", ms=10, parent_id=u1.id, is_active=False)
    a1r = _mk(conv_id, "assistant", "A1r", ms=20, parent_id=u1.id, is_active=True)
    u1e = _mk(conv_id, "user", "вопрос-ред", ms=10, parent_id=None, is_active=True)
    db_session.add_all([a1, a1r, u1e])
    await db_session.flush()
    a1e = _mk(conv_id, "assistant", "A1e", ms=30, parent_id=u1e.id, is_active=True)
    db_session.add(a1e)
    await db_session.flush()
    u1_id, a1r_id, u1e_id, a1e_id = u1.id, a1r.id, u1e.id, a1e.id
    await db_session.commit()

    # Both wirings land sequentially under the lock (the race resolved by the DB
    # lock → serialized). Edit wires its assistant first, regenerate second.
    conv = await lock_conversation(db_session, conv_id)
    await set_active_leaf_to(db_session, conv, a1e_id)  # edit's wiring
    await set_active_leaf_to(db_session, conv, a1r_id)  # regenerate's wiring (last)
    await db_session.commit()

    db_session.expire_all()
    rows = await _active_chain(db_session, conv_id)
    leaf = (
        await db_session.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    # Last writer (regenerate) owns the single coherent leaf: U1 → A1r.
    _assert_single_contiguous_path(rows, a1r_id, leaf)
    assert [m.id for m in rows] == [u1_id, a1r_id]
    # No two assistants chained without a user between: every active assistant's
    # parent is a user.
    by_id = {m.id: m for m in rows}
    for m in rows:
        if m.role == "assistant":
            assert m.parent_id is not None
            assert by_id[m.parent_id].role == "user"
    # The edit branch (U1e, A1e) is fully set aside.
    u1e_row = (
        await db_session.execute(select(Message).where(Message.id == u1e_id))
    ).scalar_one()
    a1e_row = (
        await db_session.execute(select(Message).where(Message.id == a1e_id))
    ).scalar_one()
    assert u1e_row.is_active is False
    assert a1e_row.is_active is False


async def test_restore_orphaned_answer_reverts_failed_edit_to_prior_leaf(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    """tr-yqmh: an edit that fails before producing an answer leaves the edited
    user turn active with no answer of its OWN and the prior Q&A deactivated — a
    different orphan shape than regenerate, where restore_orphaned_answer found no
    answer under the NEW user turn and no-op'd. Given the prior active leaf, it
    must revert to that coherent answered turn; the edited turn survives as an
    inactive sibling (reachable via the < n/m > switcher).
    """
    conv_id = conversation.id
    u_old = _mk(conv_id, "user", "старый вопрос", ms=0, is_active=False)
    db_session.add(u_old)
    await db_session.flush()
    a_old = _mk(conv_id, "assistant", "старый ответ", ms=10, parent_id=u_old.id, is_active=False)
    u_new = _mk(conv_id, "user", "новый вопрос", ms=20, parent_id=None, is_active=True)
    db_session.add_all([a_old, u_new])
    await db_session.flush()
    a_old_id, u_new_id = a_old.id, u_new.id
    conversation.active_leaf_id = u_new_id  # edited turn is the leaf, no answer
    await db_session.commit()

    # The new (edited) user turn has no assistant child, so restore must fall back
    # to the prior active leaf (the answer shown before the edit).
    await restore_orphaned_answer(db_session, conv_id, u_new_id, prefer_leaf_id=a_old_id)
    await db_session.commit()
    db_session.expire_all()

    rows = {
        m.content: m.is_active
        for m in (
            await db_session.execute(
                select(Message).where(Message.conversation_id == conv_id)
            )
        ).scalars()
    }
    assert rows["старый ответ"] is True   # prior answer reactivated
    assert rows["старый вопрос"] is True  # its question reactivated with it
    assert rows["новый вопрос"] is False  # edited turn parked as an inactive sibling
    leaf = (
        await db_session.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    assert leaf == a_old_id


# ── heal_orphaned_active_path (tr-5o8r) ─────────────────────────────────────


async def test_heal_orphaned_active_path_restores_stopped_regenerate(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    """tr-5o8r: a fast regenerate→Stop (~25ms abort) can skip restore_orphaned_answer
    entirely, leaving the active leaf on the parent user message with ALL assistant
    versions is_active=false — an empty-slot turn persisted in the DB. The heal
    restores the newest surviving answer AND persists the fix (leaf points at it,
    it is reactivated)."""
    conv_id = conversation.id
    u = _mk(conv_id, "user", "вопрос", ms=0)
    db_session.add(u)
    await db_session.flush()
    a_old = _mk(conv_id, "assistant", "A", ms=10, parent_id=u.id, is_active=False)
    a_new = _mk(conv_id, "assistant", "B", ms=20, parent_id=u.id, is_active=False)
    db_session.add_all([a_old, a_new])
    await db_session.flush()
    a_new_id = a_new.id
    conversation.active_leaf_id = u.id  # orphaned: leaf stranded on the user message
    await db_session.commit()

    healed = await heal_orphaned_active_path(db_session, conv_id)
    await db_session.commit()

    assert healed is True
    path = await resolve_active_path(db_session, conv_id)
    assert [m.content for m in path] == ["вопрос", "B"]  # newest answer restored
    leaf = (
        await db_session.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    assert leaf == a_new_id


async def test_heal_orphaned_active_path_noop_when_already_coherent(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    """Idempotent: a no-op when an answer is already active, so it is safe to call
    on every GET without churning the path."""
    conv_id = conversation.id
    u = _mk(conv_id, "user", "вопрос", ms=0)
    db_session.add(u)
    await db_session.flush()
    a = _mk(conv_id, "assistant", "ответ", ms=10, parent_id=u.id, is_active=True)
    db_session.add(a)
    await db_session.flush()
    a_id = a.id
    conversation.active_leaf_id = a.id
    await db_session.commit()

    healed = await heal_orphaned_active_path(db_session, conv_id)

    assert healed is False
    leaf = (
        await db_session.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    assert leaf == a_id


async def test_heal_orphaned_active_path_noop_for_unanswered_turn(
    db_session: AsyncSession, conversation: Conversation
) -> None:
    """A legitimately unanswered turn (mid-generation: a user tail with NO assistant
    children) must NOT be 'restored' — the heal only targets the all-inactive orphan
    shape, never an in-flight send."""
    conv_id = conversation.id
    u = _mk(conv_id, "user", "вопрос", ms=0)
    db_session.add(u)
    await db_session.flush()
    u_id = u.id
    conversation.active_leaf_id = u.id
    await db_session.commit()

    healed = await heal_orphaned_active_path(db_session, conv_id)

    assert healed is False
    leaf = (
        await db_session.execute(
            select(Conversation.active_leaf_id).where(Conversation.id == conv_id)
        )
    ).scalar_one()
    assert leaf == u_id
