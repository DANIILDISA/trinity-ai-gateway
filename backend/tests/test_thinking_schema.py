"""Schema tests for the extended-thinking columns (migration 013).

Verifies that reasoning_tokens (usage_logs + messages) and thinking
(messages) round-trip through Postgres, and that usage_logs.reasoning_tokens
defaults to 0 when omitted.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import Message
from app.models.usage_log import UsageLog
from app.models.user import User


async def _make_user(db: AsyncSession) -> User:
    user = User(
        email="thinking_schema@example.com",
        display_name="Thinking Schema",
        password_hash=None,
        role="user",
        status="active",
        balance_micro_rub=0,
        reserved_micro_rub=0,
    )
    db.add(user)
    await db.flush()
    return user


async def test_message_persists_thinking_and_reasoning_tokens(
    db_session: AsyncSession,
) -> None:
    user = await _make_user(db_session)
    conv = Conversation(user_id=user.id, model_id="anthropic/claude-sonnet-4-6")
    db_session.add(conv)
    await db_session.flush()

    msg = Message(
        conversation_id=conv.id,
        role="assistant",
        content="Ответ",
        model_id="anthropic/claude-sonnet-4-6",
        input_tokens=10,
        output_tokens=40,
        reasoning_tokens=25,
        thinking="Шаг 1: оценим задачу. Шаг 2: сделаем вывод.",
        cost_micro_rub=1234,
        request_id=uuid.uuid4(),
    )
    db_session.add(msg)
    await db_session.commit()
    msg_id = msg.id  # capture before expiring (avoids sync lazy-load)
    db_session.expire_all()

    fetched = (
        await db_session.execute(select(Message).where(Message.id == msg_id))
    ).scalar_one()
    assert fetched.reasoning_tokens == 25
    assert fetched.thinking == "Шаг 1: оценим задачу. Шаг 2: сделаем вывод."


async def test_usage_log_reasoning_tokens_persists_and_defaults_zero(
    db_session: AsyncSession,
) -> None:
    user = await _make_user(db_session)

    explicit = UsageLog(
        user_id=user.id,
        request_id=uuid.uuid4(),
        model_id="anthropic/claude-sonnet-4-6",
        input_tokens=10,
        output_tokens=40,
        reasoning_tokens=25,
        estimated_input_tokens=10,
        estimated_output_tokens=40,
        estimated_cost_micro_rub=100,
        actual_cost_micro_rub=100,
        reserved_micro_rub=200,
        openrouter_cost_micro_usd=0,
        exchange_rate_micro_rub=0,
    )
    omitted = UsageLog(
        user_id=user.id,
        request_id=uuid.uuid4(),
        model_id="anthropic/claude-sonnet-4-6",
        input_tokens=1,
        output_tokens=2,
        estimated_input_tokens=1,
        estimated_output_tokens=2,
        estimated_cost_micro_rub=1,
        actual_cost_micro_rub=1,
        reserved_micro_rub=1,
        openrouter_cost_micro_usd=0,
        exchange_rate_micro_rub=0,
    )
    db_session.add_all([explicit, omitted])
    await db_session.commit()
    explicit_id = explicit.id  # capture before expiring (avoids sync lazy-load)
    omitted_id = omitted.id
    db_session.expire_all()

    f_explicit = (
        await db_session.execute(select(UsageLog).where(UsageLog.id == explicit_id))
    ).scalar_one()
    f_omitted = (
        await db_session.execute(select(UsageLog).where(UsageLog.id == omitted_id))
    ).scalar_one()
    assert f_explicit.reasoning_tokens == 25
    assert f_omitted.reasoning_tokens == 0
