"""Wire contract for the thinking_segment / thinking_summary SSE events (tr-cy0.3.3).

These two events let the proxy mark reasoning-segment boundaries (`thinking_segment`)
and deliver a per-segment one-line title (`thinking_summary`) without breaking the
legacy single-segment `thinking_delta` stream. The schemas must serialize through
the existing `_sse_line` wire frame.
"""
import json

import pytest
from pydantic import ValidationError

from app.api.v1.chat import _sse_line
from app.schemas.chat import ThinkingSegmentEvent, ThinkingSummaryEvent


def test_thinking_segment_serializes_through_sse_line():
    evt = ThinkingSegmentEvent(segment_id="seg-0", index=0)
    frame = _sse_line("thinking_segment", evt.model_dump())
    assert frame == (
        "event: thinking_segment\n"
        'data: {"segment_id": "seg-0", "index": 0}\n\n'
    )
    # data line round-trips back to the original fields
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload == {"segment_id": "seg-0", "index": 0}


def test_thinking_summary_serializes_through_sse_line_utf8():
    evt = ThinkingSummaryEvent(segment_id="seg-1", summary="Разбираю три вопроса")
    frame = _sse_line("thinking_summary", evt.model_dump())
    # ensure_ascii=False keeps the Russian title readable on the wire
    assert "Разбираю три вопроса" in frame
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload == {"segment_id": "seg-1", "summary": "Разбираю три вопроса"}


def test_thinking_segment_rejects_negative_index():
    with pytest.raises(ValidationError):
        ThinkingSegmentEvent(segment_id="seg-0", index=-1)


def test_thinking_segment_rejects_empty_id():
    with pytest.raises(ValidationError):
        ThinkingSegmentEvent(segment_id="", index=0)


def test_thinking_summary_rejects_empty_fields():
    with pytest.raises(ValidationError):
        ThinkingSummaryEvent(segment_id="seg-0", summary="")
    with pytest.raises(ValidationError):
        ThinkingSummaryEvent(segment_id="", summary="ok")


# ── Tool-step SSE contract (tr-cy0.2.6) ───────────────────────────────────────

from app.schemas.chat import (  # noqa: E402
    ToolCallStartEvent,
    ToolCallArgumentsDeltaEvent,
    ToolLoopHaltedEvent,
    ToolResultEvent,
    ToolStepDTO,
)


def test_tool_call_start_serializes_through_sse_line():
    evt = ToolCallStartEvent(step_id="tc-0-0", tool="create_file", title="Создание файла a.md")
    frame = _sse_line("tool_call_start", evt.model_dump())
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload == {"step_id": "tc-0-0", "tool": "create_file", "title": "Создание файла a.md"}


def test_tool_result_serializes_with_diff():
    evt = ToolResultEvent(
        step_id="tc-0-0", status="done", filename="a.md",
        diff={"added": 42, "removed": 1}, presented=True, output="ok",
    )
    frame = _sse_line("tool_result", evt.model_dump())
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload["diff"] == {"added": 42, "removed": 1}
    assert payload["status"] == "done" and payload["presented"] is True


def test_tool_args_delta_and_halted():
    a = ToolCallArgumentsDeltaEvent(step_id="tc-0-0", delta='{"path":')
    assert a.model_dump()["delta"] == '{"path":'
    h = ToolLoopHaltedEvent(reason="max_rounds")
    assert h.model_dump() == {"reason": "max_rounds"}


def test_tool_result_rejects_bad_status():
    with pytest.raises(Exception):
        ToolResultEvent(step_id="s", status="running")  # running is not a result status


def test_tool_step_dto_round_trips():
    step = ToolStepDTO(
        id="tc-0-0", tool="create_file", title="Создание файла a.md",
        filename="a.md", diff={"added": 2, "removed": 0}, presented=True, status="done",
    )
    assert step.model_dump()["diff"] == {"added": 2, "removed": 0}
