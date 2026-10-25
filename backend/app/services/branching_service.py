"""Conversation branching helpers (message version history).

The shared layer used by send / regenerate / edit / switch so SSE
orchestration never drifts on active-path bookkeeping. Every function preserves
the (created_at ASC, id ASC) ordering and the `is_active = true AND
deleted_at IS NULL` active-branch filter used by the context reads
(message_service.get_conversation_messages, chat_image_service.build_history_with_images).

Siblings = versions of one turn = rows sharing (parent_id, role).
"""
import uuid
from collections import defaultdict

from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import Message


async def lock_conversation(
    db: AsyncSession, conversation_id: uuid.UUID
) -> Conversation:
    """SELECT ... FOR UPDATE the conversation row, serializing concurrent
    branch mutations (send STEP-7, regenerate, edit, switch) so exactly one
    active leaf survives a race (design §8 concurrency)."""
    return (
        await db.execute(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .with_for_update()
        )
    ).scalar_one()


async def resolve_active_path(
    db: AsyncSession, conversation_id: uuid.UUID
) -> list[Message]:
    """The rendered root->leaf active path, ordered (created_at ASC, id ASC).

    When the conversation has an active_leaf_id, the path is the set of active,
    non-deleted nodes (one branch by construction — exactly one active leaf).
    Legacy rows (active_leaf_id IS NULL, pre-014) fall back to plain created_at
    ordering of the active, non-deleted nodes — identical to the pre-feature
    transcript (design §3, §8 legacy NULL fallback).
    """
    rows = list(
        (
            await db.execute(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.is_active.is_(True),
                    Message.deleted_at.is_(None),
                )
                .order_by(Message.created_at.asc(), Message.id.asc())
            )
        ).scalars()
    )
    return rows


async def deactivate_active_tail_from(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    from_message: Message,
) -> None:
    """Set is_active=false on `from_message` and every active node after it on
    the active path. Used by regenerate/edit to set the stale continuation aside
    (never deleted) before stacking the new sibling (design §5).

    The boundary is a (created_at, id) row-value comparison, matching the read
    ordering used everywhere (resolve_active_path / get_conversation_messages).
    A plain `created_at >= ...` would over-deactivate same-`created_at` siblings:
    edit/regenerate co-create the new user+assistant nodes in one transaction, so
    they share `created_at` (server-side now() = txn start), and a scalar
    comparison can't tell the boundary node from its co-created sibling. The
    tuple comparison breaks the tie on id, leaving earlier siblings active."""
    await db.execute(
        update(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.is_active.is_(True),
            Message.deleted_at.is_(None),
            tuple_(Message.created_at, Message.id)
            >= tuple_(from_message.created_at, from_message.id),
        )
        .values(is_active=False)
    )


async def repoint_active_leaf(
    db: AsyncSession,
    conversation: Conversation,
    leaf_message_id: uuid.UUID,
) -> None:
    """Denormalize the active path's tail onto conversations.active_leaf_id, in
    the same transaction as the is_active change that produced it (design §3)."""
    conversation.active_leaf_id = leaf_message_id
    await db.flush()


async def set_active_leaf_to(
    db: AsyncSession,
    conversation: Conversation,
    leaf_message_id: uuid.UUID,
) -> None:
    """Establish EXACTLY one contiguous active path ending at `leaf_message_id`.

    Idempotent and self-healing: regardless of the prior is_active state (even a
    corrupt two-leaf split), this leaves the active set equal to the single
    root→leaf chain through `leaf_message_id`. It is the unifying primitive behind
    send STEP-7, regenerate, edit, and the free version switch — the last writer
    under `lock_conversation` wins coherently, so a race between two branch
    mutations can never leave two active leaves (design §8 invariant).

    Steps, in order:
      1. Deactivate ALL currently-active messages in the conversation.
      2. Walk UP from `leaf_message_id` via parent_id over non-deleted nodes,
         collecting the chain leaf→…→root, and re-activate every node on it.
      3. Denormalize the tail onto conversation.active_leaf_id.

    The deactivate MUST precede the re-activation walk: doing it after would flip
    the freshly-activated chain back off.
    """
    await db.execute(
        update(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.is_active.is_(True),
        )
        .values(is_active=False)
    )

    node = await db.get(Message, leaf_message_id)
    while node is not None and node.deleted_at is None:
        node.is_active = True
        parent_id = node.parent_id
        node = await db.get(Message, parent_id) if parent_id is not None else None

    conversation.active_leaf_id = leaf_message_id
    await db.flush()


async def restore_orphaned_answer(
    db: AsyncSession,
    conversation_id: uuid.UUID | None,
    parent_id: uuid.UUID | None,
    prefer_leaf_id: uuid.UUID | None = None,
) -> None:
    """Re-activate an answered turn that a failed regenerate/edit left orphaned
    (tr-hvr1).

    regenerate/edit deactivate the prior answer and repoint the active leaf to the
    parent user message BEFORE the replacement exists, and commit that. If the
    stream then yields no assistant version (Stop before content, OpenRouter error,
    timeout), every sibling of that turn stays is_active=false and the leaf points
    at a node with no active assistant child — so the answered turn renders as an
    empty slot on reload (visible data loss), even though the content rows survive.

    Restores the answer that was on screen when the user regenerated to a coherent
    active path. `prefer_leaf_id` is that exact version (the regenerated target); a
    user may have switched the < n/m > selector to a non-newest version before
    regenerating, so restoring the newest sibling would silently show a different
    answer. We restore `prefer_leaf_id` when it is still a live sibling, and fall
    back to the newest surviving answer only if it is unknown or gone (deleted).

    It is a NO-OP when the turn legitimately has no answer AND no prior leaf was
    given (a plain failed send: `parent_id` has no assistant children and
    `prefer_leaf_id` is None), or when an answer is already active (the success path
    already wired a coherent leaf), so it is safe to call unconditionally from every
    failure path. A failed EDIT passes the prior active leaf (which lives under a
    DIFFERENT user sibling), so the answered turn it replaced is reverted to (tr-yqmh).
    """
    if conversation_id is None or parent_id is None:
        return

    children = list(
        (
            await db.execute(
                select(Message.id, Message.is_active)
                .where(
                    Message.parent_id == parent_id,
                    Message.role == "assistant",
                    Message.deleted_at.is_(None),
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
            )
        ).all()
    )
    if not children:
        # tr-yqmh: a FAILED EDIT is a different orphan shape. The new (edited) user
        # turn has no answer of its own, so there is no sibling answer to restore
        # UNDER it. If the caller passed the prior active leaf (the answer shown
        # before the edit), revert the active path to it so the answered turn isn't
        # stranded behind the empty edited turn; the edited turn survives as an
        # inactive sibling, reachable via the < n/m > switcher. A plain failed send
        # passes no prefer_leaf_id, so it stays a no-op (nothing to restore).
        if prefer_leaf_id is None:
            return
        prior = await db.get(Message, prefer_leaf_id)
        if (
            prior is None
            or prior.conversation_id != conversation_id
            or prior.deleted_at is not None
        ):
            return
        conversation = await lock_conversation(db, conversation_id)
        await set_active_leaf_to(db, conversation, prefer_leaf_id)
        return
    if any(is_active for _mid, is_active in children):
        return  # an answer is already active — the path is already coherent

    child_ids = [mid for mid, _is_active in children]
    restore_id = prefer_leaf_id if prefer_leaf_id in child_ids else child_ids[0]
    conversation = await lock_conversation(db, conversation_id)
    await set_active_leaf_to(db, conversation, restore_id)


async def heal_orphaned_active_path(
    db: AsyncSession, conversation_id: uuid.UUID
) -> bool:
    """Persistently repair an active path orphaned by a fast regenerate/edit→Stop.

    The regenerate/edit endpoint deactivates the prior answer and repoints the
    active leaf to the parent user message BEFORE the replacement exists, and
    commits that. On success STEP-7 advances the leaf to the new answer; on a
    normal failure restore_orphaned_answer reverts it. But a sub-~25ms abort can
    cancel the stream BEFORE process_chat_request installs its cancellation
    handler, so restore_orphaned_answer never runs and the orphaning commit is
    permanent: the active leaf sits on a user message whose assistant children are
    ALL is_active=false — an empty-slot turn, plus latent corruption of the
    persisted path and the < n/m > index (tr-5o8r).

    This re-runs that restore on a clean read request (GET conversation), well
    outside the cancellation minefield where it can safely commit. Idempotent: a
    no-op when the path already ends at an active answer, OR when the turn
    legitimately has no answer yet (in-flight generation = a user tail with no
    assistant children — never touched). Returns True iff it restored an orphaned
    answer; the caller commits.
    """
    path = await resolve_active_path(db, conversation_id)
    if not path or path[-1].role != "user":
        return False
    tail_user_id = path[-1].id

    children = list(
        (
            await db.execute(
                select(Message.is_active).where(
                    Message.parent_id == tail_user_id,
                    Message.role == "assistant",
                    Message.deleted_at.is_(None),
                )
            )
        ).all()
    )
    # No children → unanswered/in-flight; any still active → already coherent. Only
    # the all-inactive shape is the tr-5o8r orphan worth repairing.
    if not children or any(is_active for (is_active,) in children):
        return False

    await restore_orphaned_answer(db, conversation_id, tail_user_id, None)
    return True


async def derive_version_info(
    db: AsyncSession, conversation_id: uuid.UUID
) -> dict[uuid.UUID, tuple[int, int]]:
    """message_id -> (version_index, version_count).

    Siblings (versions of one turn) share (parent_id, role); within a group the
    1-based ordinal by (created_at ASC, id ASC) is version_index and the group
    size is version_count. Spans ALL siblings incl. inactive, so the < n/m >
    switcher can page every version. Legacy single-version turns => (1, 1)
    everywhere, which auto-hides the switcher (design §6).
    """
    rows = list(
        (
            await db.execute(
                select(Message.id, Message.parent_id, Message.role)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.asc(), Message.id.asc())
            )
        ).all()
    )

    groups: dict[tuple[uuid.UUID | None, str], list[uuid.UUID]] = defaultdict(list)
    for msg_id, parent_id, role in rows:
        groups[(parent_id, role)].append(msg_id)

    info: dict[uuid.UUID, tuple[int, int]] = {}
    for members in groups.values():
        count = len(members)
        for index, msg_id in enumerate(members, start=1):
            info[msg_id] = (index, count)
    return info
