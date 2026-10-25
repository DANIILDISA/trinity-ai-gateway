"""MessageResponse serialization of thinking_segments for reload (tr-cy0.3.16).

_to_message_response is the single serializer behind GET /conversations/{id} and
the active-path switch, so verifying it directly proves the reload path carries
the per-segment timeline (and degrades to NULL for legacy rows) without standing
up the full HTTP stack.
"""
import uuid
from datetime import datetime, timezone

from app.api.v1.chat import _to_message_response
from app.models.message import Message


def _assistant(thinking_segments) -> Message:
    return Message(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        role="assistant",
        content="ответ",
        model_id="m",
        input_tokens=1,
        output_tokens=1,
        reasoning_tokens=1,
        cost_micro_rub=1,
        request_id=uuid.uuid4(),
        thinking="t",
        thinking_segments=thinking_segments,
        created_at=datetime.now(timezone.utc),
        is_active=True,
    )


def test_to_message_response_round_trips_thinking_segments():
    segs = [
        {"id": "seg-0", "text": "Анализ", "summary": "Анализирую вопросы"},
        {"id": "seg-1", "text": "Ответ", "summary": None},
    ]
    resp = _to_message_response(_assistant(segs))
    assert [s.model_dump() for s in resp.thinking_segments] == segs


def test_to_message_response_legacy_null_thinking_segments():
    resp = _to_message_response(_assistant(None))
    assert resp.thinking_segments is None


def _assistant_with_steps(tool_steps) -> Message:
    return Message(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        role="assistant",
        content="готово",
        model_id="m",
        input_tokens=1,
        output_tokens=1,
        reasoning_tokens=1,
        cost_micro_rub=1,
        request_id=uuid.uuid4(),
        tool_steps=tool_steps,
        created_at=datetime.now(timezone.utc),
        is_active=True,
    )


def test_to_message_response_round_trips_tool_steps():
    steps = [
        {"id": "tc-0-0", "tool": "create_file", "title": "Создание файла a.md",
         "filename": "a.md", "diff": {"added": 2, "removed": 0}, "presented": False,
         "status": "done", "output": "ok"},
    ]
    resp = _to_message_response(_assistant_with_steps(steps))
    # Round-trip the SUBMITTED fields; the DTO also carries run_python/rich-output
    # defaults (stderr/duration_ms/truncated/input/results = None) that a file-tool
    # step legitimately omits — compare on the input keys so those defaults don't make
    # the assertion brittle (tr-ytx6: strict == has silently failed since tr-jk4j).
    dumped = [s.model_dump() for s in resp.tool_steps]
    assert [{k: d[k] for k in steps[i]} for i, d in enumerate(dumped)] == steps


def test_to_message_response_null_tool_steps():
    assert _to_message_response(_assistant_with_steps(None)).tool_steps is None
