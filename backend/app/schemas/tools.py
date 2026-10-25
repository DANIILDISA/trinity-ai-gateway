"""Tool-call argument + result schemas and OpenAI function-calling definitions
for the agentic file tools (tr-cy0.2.2).

The model is given these definitions (only when tools are enabled AND the model
advertises supports_tools); Trinity validates the arguments it streams back
against the *Args schemas before executing. `ToolResultPayload.output` is the
text fed back to the model as the `tool` message; the other fields drive the
live timeline / artifact card.
"""
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError

# Per-file size cap for a model-authored artifact (mirrors architecture §7.4).
MAX_ARTIFACT_BYTES = 1_000_000
# Max size of a code snippet the model may submit to run_python (epic tr-s3y1).
MAX_CODE_BYTES = 100_000
# A logical artifact name (not a real path): word chars, space, dot, dash, slash.
_PATH_RE = re.compile(r"^[\w .\-/]{1,255}$")


def _validate_path(v: str) -> str:
    # Reject traversal explicitly and constrain to the safe charset — the "path"
    # is a logical artifact key, never a real filesystem path.
    if ".." in v or not _PATH_RE.match(v):
        raise PydanticCustomError(
            "invalid_path", "Недопустимое имя файла"
        )
    return v


class CreateFileArgs(BaseModel):
    path: str = Field(min_length=1, max_length=255)
    file_text: str = Field(max_length=MAX_ARTIFACT_BYTES)

    @field_validator("path")
    @classmethod
    def _v_path(cls, v: str) -> str:
        return _validate_path(v)

    @field_validator("file_text")
    @classmethod
    def _v_size(cls, v: str) -> str:
        if len(v.encode("utf-8")) > MAX_ARTIFACT_BYTES:
            raise PydanticCustomError(
                "file_too_large",
                "Файл превышает лимит {max} байт",
                {"max": MAX_ARTIFACT_BYTES},
            )
        return v


class StrReplaceArgs(BaseModel):
    path: str = Field(min_length=1, max_length=255)
    # old_str must be non-empty (it has to occur exactly once in the file).
    old_str: str = Field(min_length=1)
    new_str: str = Field(default="", max_length=MAX_ARTIFACT_BYTES)

    @field_validator("path")
    @classmethod
    def _v_path(cls, v: str) -> str:
        return _validate_path(v)


class ViewFileArgs(BaseModel):
    # Read-only: return the current content of an existing workspace file so the
    # model can edit it across turns without guessing (tr-7ml).
    path: str = Field(min_length=1, max_length=255)

    @field_validator("path")
    @classmethod
    def _v_path(cls, v: str) -> str:
        return _validate_path(v)


class RunPythonArgs(BaseModel):
    # The Python source the model wants executed in the sandbox (epic tr-s3y1).
    code: str = Field(min_length=1, max_length=MAX_CODE_BYTES)


class ToolDiff(BaseModel):
    added: int
    removed: int


class ToolResultPayload(BaseModel):
    status: Literal["ok", "error"]
    filename: str | None = None
    diff: ToolDiff | None = None
    presented: bool = False
    # Text fed back to the model as the `tool` message content.
    output: str
    # Code-execution metadata (epic tr-s3y1 Phase 2). Defaults keep every existing
    # file-tool result back-compatible (they leave these unset).
    stderr: str | None = None
    duration_ms: int | None = None
    truncated: bool = False
    # Ruble cost (micro-rubles) the executor computed — kept for a future
    # cost-recovery decision. Under Decision A (spec 2026-06-26 §1) the sandbox fee
    # is 0, so this evaluates to 0 and is NOT charged; execution is billed via the
    # tokens its code+output consume (metered by the existing budget_guard). Turning
    # on a real fee needs new accumulation code in the tool_result handler, not just
    # a config change. File tools leave it 0.
    cost_micro_rub: int = 0
    # Rich outputs (epic tr-s3y1 Phase 4, tr-5ktl): tagged results captured in the
    # sandbox — {"type": "image/png"|"text/html"|"text/plain", "data": ...}. Carried
    # to the run-card; image/png entries are also persisted as downloadable artifacts.
    # File tools leave this empty.
    results: list = Field(default_factory=list)


# ── OpenAI/OpenRouter function-calling definitions ───────────────────────────

CREATE_FILE_DEFINITION = {
    "type": "function",
    "function": {
        "name": "create_file",
        "description": (
            "Create a new file in the user's workspace with the given text. Use "
            "this when the user asks you to generate a document/file. The file is "
            "then presented to the user as a downloadable artifact."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File name, e.g. python_exam_prep.md",
                },
                "file_text": {
                    "type": "string",
                    "description": "Full text content of the file.",
                },
            },
            "required": ["path", "file_text"],
        },
    },
}

STR_REPLACE_DEFINITION = {
    "type": "function",
    "function": {
        "name": "str_replace",
        "description": (
            "Edit an existing file by replacing an exact, unique substring with new "
            "text. old_str must occur exactly once in the current file. If you are "
            "unsure of the file's current content (e.g. it was created in an earlier "
            "turn), call view_file first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File name to edit."},
                "old_str": {
                    "type": "string",
                    "description": "Exact text to replace (must be unique in the file).",
                },
                "new_str": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
}

RUN_PYTHON_DEFINITION = {
    "type": "function",
    "function": {
        "name": "run_python",
        "description": (
            "Execute Python 3 code in a secure, isolated sandbox and get back its "
            "stdout/stderr plus any rich outputs. Use this whenever you need to actually "
            "RUN code to compute a result, verify logic, parse/transform data, do math, "
            "plot, or test a snippet — instead of guessing the output. The session is "
            "PERSISTENT per conversation: variables, imports and files you create persist "
            "across calls (it may reset after a timeout or long idle — you will be told if "
            "so). numpy, pandas and matplotlib are preinstalled; there is NO network access "
            "and a short wall-clock limit. Files the user attached to this conversation are "
            "available to read at 'inputs/<filename>'. Any file you write to the working "
            "directory is returned to the user as a downloadable file, and matplotlib "
            "figures (plus a trailing DataFrame expression) are shown inline. Print the "
            "results you need; only stdout/stderr and those rich outputs are returned. On a "
            "traceback, read the error and try again."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The Python source to execute. Print what you need to see.",
                },
            },
            "required": ["code"],
        },
    },
}

VIEW_FILE_DEFINITION = {
    "type": "function",
    "function": {
        "name": "view_file",
        "description": (
            "Read and return the current full content of an existing file in the "
            "workspace. Call this before editing a file you did not just create in "
            "this same turn (e.g. a file from an earlier turn), so str_replace uses "
            "text that actually exists. Read-only — it changes nothing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File name to read."},
            },
            "required": ["path"],
        },
    },
}
