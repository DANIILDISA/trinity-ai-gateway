"""Context-window truncation tests for token_estimator.truncate_context."""
import pytest

from app.core.exceptions import ContextTooLongError
from app.services.token_estimator import (
    count_message_tokens,
    truncate_context,
)


def test_no_truncation_needed() -> None:
    messages = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello! How can I help?"},
        {"role": "user", "content": "What's 2 + 2?"},
    ]
    result = truncate_context(messages, context_window=8192, reserve_tokens=1000)

    assert result.was_truncated is False
    assert result.messages == messages
    assert result.dropped_count == 0
    assert result.original_token_count == count_message_tokens(messages)
    assert result.final_token_count == result.original_token_count


def test_history_truncated() -> None:
    # 50 short messages, alternating user/assistant, tight context.
    messages = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"Message number {i} discussing the weather today.",
        }
        for i in range(50)
    ]
    # Each message is ~11-13 tokens; full set ~600 tokens.
    # Set budget ≈ 100 → only a handful of newest messages survive.
    result = truncate_context(messages, context_window=150, reserve_tokens=50)

    assert result.was_truncated is True
    assert result.dropped_count > 0
    # The last message must be retained.
    assert result.messages[-1] == messages[-1]
    # Surviving count strictly less than 50.
    assert len(result.messages) < 50
    # Budget is respected.
    assert result.final_token_count <= 100


def test_system_message_preserved() -> None:
    system = {"role": "system", "content": "You are a helpful Russian-speaking assistant."}
    history = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"History turn {i} about Moscow weather discussion.",
        }
        for i in range(30)
    ]
    last = {"role": "user", "content": "What did we discuss?"}
    messages = [system, *history, last]

    # Force truncation of mid-history.
    result = truncate_context(messages, context_window=200, reserve_tokens=50)

    assert result.was_truncated is True
    # System still present and still first.
    assert result.messages[0] == system
    # Last user message still present and still last.
    assert result.messages[-1] == last


def test_system_message_sacrificed() -> None:
    # System + last together exceed budget; last alone fits.
    system = {"role": "system", "content": "sys"}  # tiny
    # Build a user message that's ~80 content tokens so that
    # [system, last] count > budget but [last] alone fits.
    long_content = "word " * 80  # ~80 tokens
    last = {"role": "user", "content": long_content}
    messages = [system, last]

    tokens_with_system = count_message_tokens([system, last])
    tokens_last_only = count_message_tokens([last])
    # Choose budget strictly between them.
    budget = tokens_last_only + 1
    assert tokens_with_system > budget > tokens_last_only

    # Set context_window so context_window - reserve_tokens == budget.
    reserve = 0
    context_window = budget + reserve

    result = truncate_context(
        messages, context_window=context_window, reserve_tokens=reserve
    )

    assert result.was_truncated is True
    # System message dropped.
    assert all(m.get("role") != "system" for m in result.messages)
    # Last preserved.
    assert result.messages[-1] == last
    assert result.dropped_count == 1


def test_last_message_always_kept() -> None:
    # Multiple truncation regimes — last must survive each.
    last = {"role": "user", "content": "ANCHOR"}

    # Regime A: no truncation needed
    msgs_a = [{"role": "user", "content": "x"}, last]
    res_a = truncate_context(msgs_a, context_window=8192, reserve_tokens=1000)
    assert res_a.messages[-1] == last

    # Regime B: drop mid-history
    msgs_b = [
        {"role": "system", "content": "sys"},
        *[
            {"role": "user", "content": f"older turn {i} {('blabla ') * 5}"}
            for i in range(40)
        ],
        last,
    ]
    res_b = truncate_context(msgs_b, context_window=200, reserve_tokens=50)
    assert res_b.messages[-1] == last

    # Regime C: drop system too
    long_content = "word " * 80
    long_last = {"role": "user", "content": long_content}
    msgs_c = [{"role": "system", "content": "sys"}, long_last]
    only_last = count_message_tokens([long_last])
    res_c = truncate_context(msgs_c, context_window=only_last + 1, reserve_tokens=0)
    assert res_c.messages[-1] == long_last


def test_single_message_too_long() -> None:
    # Build a single user message large enough that even alone it blows the
    # budget. budget = 200 - 50 = 150; "word " * 1000 ≈ 1000 tokens.
    huge = {"role": "user", "content": "word " * 1000}

    with pytest.raises(ContextTooLongError):
        truncate_context([huge], context_window=200, reserve_tokens=50)


def test_original_token_count_accurate() -> None:
    messages = [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "How does HMAC work?"},
        {"role": "assistant", "content": "HMAC combines a key with a hash..."},
        {"role": "user", "content": "Кратко на русском, пожалуйста."},
    ]
    expected = count_message_tokens(messages)

    # No truncation path
    res1 = truncate_context(messages, context_window=8192, reserve_tokens=1000)
    assert res1.original_token_count == expected

    # Truncation path
    res2 = truncate_context(messages, context_window=80, reserve_tokens=20)
    assert res2.original_token_count == expected
