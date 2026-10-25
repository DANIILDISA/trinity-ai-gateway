"""Tool contract: line diff, arg validation, registry (tr-cy0.2.2)."""
import pytest
from pydantic import ValidationError

from app.schemas.tools import (
    MAX_ARTIFACT_BYTES,
    CreateFileArgs,
    StrReplaceArgs,
    ToolResultPayload,
)
from app.services.tools.diff import line_diff
from app.services.tools.registry import ToolSpec, clear_registry, definitions, get, register


# ── line_diff ────────────────────────────────────────────────────────────────


def test_line_diff_create_counts_all_added():
    assert line_diff("", "a\nb\nc") == {"added": 3, "removed": 0}


def test_line_diff_replace():
    assert line_diff("a\nb\nc", "a\nX\nc") == {"added": 1, "removed": 1}


def test_line_diff_insert():
    assert line_diff("a\nb", "a\nb\nc\nd") == {"added": 2, "removed": 0}


def test_line_diff_delete():
    assert line_diff("a\nb\nc", "a\nc") == {"added": 0, "removed": 1}


# ── arg validation ───────────────────────────────────────────────────────────


def test_create_file_args_ok():
    a = CreateFileArgs(path="python_exam_prep.md", file_text="# Hi")
    assert a.path == "python_exam_prep.md"


def test_create_file_rejects_path_traversal():
    with pytest.raises(ValidationError):
        CreateFileArgs(path="../etc/passwd", file_text="x")
    with pytest.raises(ValidationError):
        CreateFileArgs(path="a/../../b", file_text="x")


def test_create_file_rejects_oversize():
    with pytest.raises(ValidationError):
        CreateFileArgs(path="big.md", file_text="x" * (MAX_ARTIFACT_BYTES + 1))


def test_str_replace_requires_nonempty_old():
    with pytest.raises(ValidationError):
        StrReplaceArgs(path="a.md", old_str="", new_str="x")
    ok = StrReplaceArgs(path="a.md", old_str="foo", new_str="bar")
    assert ok.old_str == "foo"


def test_tool_result_payload_shape():
    r = ToolResultPayload(
        status="ok",
        filename="a.md",
        diff={"added": 2, "removed": 0},
        presented=True,
        output="created a.md",
    )
    assert r.status == "ok"
    assert r.diff.added == 2


# ── registry ─────────────────────────────────────────────────────────────────


def test_registry_register_get_definitions():
    clear_registry()
    try:
        async def _dummy(args, ctx):
            return ToolResultPayload(status="ok", output="ok")

        spec = ToolSpec(
            name="t",
            definition={"type": "function", "function": {"name": "t"}},
            executor=_dummy,
            safety_profile="data_only",
        )
        register(spec)
        assert get("t") is spec
        assert get("missing") is None
        assert definitions() == [{"type": "function", "function": {"name": "t"}}]
    finally:
        clear_registry()


# ── run_python schema + billing config (Phase 2, tr-m232) ────────────────────


def test_run_python_args_ok():
    from app.schemas.tools import RunPythonArgs

    a = RunPythonArgs(code="print(123 / 6)")
    assert a.code == "print(123 / 6)"


def test_run_python_args_rejects_empty():
    from app.schemas.tools import RunPythonArgs

    with pytest.raises(ValidationError):
        RunPythonArgs(code="")


def test_run_python_args_rejects_oversize():
    from app.schemas.tools import MAX_CODE_BYTES, RunPythonArgs

    with pytest.raises(ValidationError):
        RunPythonArgs(code="x" * (MAX_CODE_BYTES + 1))


def test_run_python_definition_shape():
    from app.schemas.tools import RUN_PYTHON_DEFINITION

    fn = RUN_PYTHON_DEFINITION["function"]
    assert fn["name"] == "run_python"
    assert fn["parameters"]["required"] == ["code"]
    assert "code" in fn["parameters"]["properties"]


def test_tool_result_payload_execution_fields_default_back_compat():
    r = ToolResultPayload(status="ok", output="x")
    assert r.duration_ms is None
    assert r.stderr is None
    assert r.truncated is False
    assert r.cost_micro_rub == 0


def test_tool_result_payload_carries_execution_fields():
    r = ToolResultPayload(
        status="error",
        output="boom",
        stderr="Traceback...",
        duration_ms=1234,
        truncated=True,
        cost_micro_rub=7300,
    )
    assert r.duration_ms == 1234
    assert r.stderr == "Traceback..."
    assert r.truncated is True
    assert r.cost_micro_rub == 7300


def test_tool_result_event_and_dto_carry_execution_metadata():
    # tr-pock: the SSE event + persisted DTO carry run_python's duration/stderr/
    # truncated so the run-card renders (live and on reload).
    from app.schemas.chat import ToolResultEvent, ToolStepDTO

    ev = ToolResultEvent(
        step_id="tc-0-0", status="error", output="boom",
        stderr="Traceback", duration_ms=1234, truncated=True,
    )
    assert ev.duration_ms == 1234
    assert ev.stderr == "Traceback"
    assert ev.truncated is True

    dto = ToolStepDTO(
        id="tc-0-0", tool="run_python", title="Выполнение Python",
        status="done", duration_ms=50, stderr=None, truncated=False,
    )
    assert dto.duration_ms == 50
    assert dto.truncated is False


def test_sandbox_billing_config_defaults():
    from app.config import Settings

    # Decision A (spec 2026-06-26 §1): code execution is billed via tokens only —
    # no separate sandbox fee. The knobs stay (future cost-recovery) but default 0.
    assert Settings.model_fields["sandbox_run_base_micro_rub"].default == 0
    assert Settings.model_fields["sandbox_sec_micro_rub"].default == 0


def test_agentic_system_prompt_covers_tools_and_untrusted_framing():
    from app.services.tools.prompt import AGENTIC_SYSTEM_PROMPT

    assert "create_file" in AGENTIC_SYSTEM_PROMPT
    assert "str_replace" in AGENTIC_SYSTEM_PROMPT
    # The untrusted-data / no-instruction framing (architecture §7.1) is present.
    assert "UNTRUSTED" in AGENTIC_SYSTEM_PROMPT
    assert "never as instructions" in AGENTIC_SYSTEM_PROMPT


def test_agentic_system_prompt_authors_via_tool_not_inline():
    """tr-aeb: must instruct authoring the file via the tool (not inline) and
    creating/finalizing the file BEFORE writing the prose answer."""
    from app.services.tools.prompt import AGENTIC_SYSTEM_PROMPT

    # Author content via create_file rather than pasting it inline in the reply.
    assert "AUTHOR THE CONTENT VIA create_file" in AGENTIC_SYSTEM_PROMPT
    assert "do NOT paste the full document contents inline" in AGENTIC_SYSTEM_PROMPT
    # File tool(s) FIRST, then the brief answer (artifact exists before prose).
    assert "Call the file tool(s) FIRST" in AGENTIC_SYSTEM_PROMPT
    assert "THEN" in AGENTIC_SYSTEM_PROMPT
