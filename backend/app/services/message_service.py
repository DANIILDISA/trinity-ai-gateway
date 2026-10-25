"""Message persistence and conversation-history reads (Task 6.2)."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import Message


async def _touch_conversation(db: AsyncSession, conversation_id: uuid.UUID) -> None:
    """Bump conversations.updated_at so list-by-recency reflects activity."""
    await db.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(updated_at=datetime.now(timezone.utc))
    )


async def save_user_message(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    content: str,
    *,
    parent_id: uuid.UUID | None = None,
) -> Message:
    """Persist a user-authored message. Token/cost fields stay NULL.

    `parent_id` links this turn into the branch tree: a normal new send parents
    off the conversation's current active leaf (the prior active assistant
    message), an edit/resend parents off the message it replaces (siblings share
    a parent). NULL = the first turn (root). New user nodes are always `is_active`
    — they land on the rendered active path. Bumps the conversation's updated_at
    so it surfaces at the top of the user's list (SCHEMA.md §3.6).
    """
    msg = Message(
        conversation_id=conversation_id,
        role="user",
        content=content,
        parent_id=parent_id,
        is_active=True,
    )
    db.add(msg)
    await _touch_conversation(db, conversation_id)
    await db.flush()
    return msg


async def save_assistant_message(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    content: str,
    model_id: str,
    request_id: uuid.UUID,
    input_tokens: int,
    output_tokens: int,
    cost_micro_rub: int,
    reasoning_tokens: int | None = None,
    thinking: str | None = None,
    thinking_segments: list | None = None,
    tool_steps: list | None = None,
) -> Message:
    """Persist an assistant response with the billing data it generated.

    cost_micro_rub is the actual debited amount from finalization — when
    overdraft clamping fires, this is the (clamped) amount actually charged,
    not the gross calculated cost (see BILLING_LOGIC.md §6.1).

    reasoning_tokens/thinking carry the extended-thinking split + text (both
    None when thinking was off) so reloads can restore the token meta and the
    collapsible thinking block.
    """
    msg = Message(
        conversation_id=conversation_id,
        role="assistant",
        content=content,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cost_micro_rub=cost_micro_rub,
        request_id=request_id,
        thinking=thinking,
        thinking_segments=thinking_segments,
        tool_steps=tool_steps,
    )
    db.add(msg)
    await _touch_conversation(db, conversation_id)
    await db.flush()
    return msg


async def get_conversation_messages(
    db: AsyncSession, conversation_id: uuid.UUID | None
) -> list[dict[str, str]]:
    """Return conversation history as the {role, content} dicts the model expects.

    Returns [] when:
      - conversation_id is None (ephemeral / first-turn chat)
      - the conversation was soft-deleted
      - the conversation has no user/assistant messages

    Skips system messages — Trinity doesn't surface them yet and the chat API
    doesn't accept them, so they shouldn't be replayed into the model context.
    Stable order: created_at ASC, id ASC (matches GET /conversations/{id}).

    Only the ACTIVE branch is returned: rows with is_active = true AND
    deleted_at IS NULL. Superseded regenerate/edit siblings and soft-deleted
    nodes are excluded so inactive versions never enter (or get billed into)
    model context. build_history_with_images applies the identical filter.
    """
    if conversation_id is None:
        return []

    conv = (
        await db.execute(
            select(Conversation.id).where(
                Conversation.id == conversation_id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        return []

    rows = (
        await db.execute(
            select(Message.role, Message.content)
            .where(
                Message.conversation_id == conversation_id,
                Message.role.in_(["user", "assistant"]),
                Message.is_active.is_(True),
                Message.deleted_at.is_(None),
            )
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    ).all()
    return [{"role": role, "content": content} for role, content in rows]
