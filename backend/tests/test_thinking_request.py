"""Contract tests for the extended-thinking fields on SendMessageRequest."""
import pytest
from pydantic import ValidationError

from app.schemas.chat import SendMessageRequest


def test_thinking_defaults_off_medium() -> None:
    req = SendMessageRequest(content="привет", model="anthropic/claude-sonnet-4-6")
    assert req.thinking_enabled is False
    assert req.thinking_effort == "medium"


def test_thinking_fields_accepted() -> None:
    req = SendMessageRequest(
        content="привет",
        model="anthropic/claude-sonnet-4-6",
        thinking_enabled=True,
        thinking_effort="high",
    )
    assert req.thinking_enabled is True
    assert req.thinking_effort == "high"


def test_invalid_effort_rejected() -> None:
    with pytest.raises(ValidationError):
        SendMessageRequest(
            content="привет",
            model="anthropic/claude-sonnet-4-6",
            thinking_effort="extreme",
        )
