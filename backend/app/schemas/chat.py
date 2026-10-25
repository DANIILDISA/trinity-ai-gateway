from datetime import datetime
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError

_MAX_TEXT_LEN = 32000
_MAX_IMAGES = 4
# Total attachments (images + files combined) per message — mirrors the
# frontend's MAX_ATTACHMENTS (ChatWindow §3).
_MAX_ATTACHMENTS = 4
# A 5 MB image is ~6.9 MB of base64; cap the data-URI well above that but
# bounded, so a malformed request can't exhaust memory parsing JSON.
_MAX_IMAGE_URL_LEN = 8_000_000
# A 10 MB file is ~13.4 MB of base64; cap the data-URI (incl. the
# `data:<mime>;base64,` prefix) a little above that, but bounded.
_MAX_FILE_DATA_LEN = 14_000_000
_MAX_FILENAME_LEN = 255


class ConversationCreate(BaseModel):
    model_id: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=255)


class ConversationRename(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class TextContentPart(BaseModel):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=_MAX_TEXT_LEN)


class ImageUrlPayload(BaseModel):
    # data:{mediaType};base64,{...}
    url: str = Field(min_length=1, max_length=_MAX_IMAGE_URL_LEN)


class ImageContentPart(BaseModel):
    type: Literal["image_url"]
    image_url: ImageUrlPayload


class FilePayload(BaseModel):
    # Original filename, surfaced to the model and the file-parser plugin.
    filename: str = Field(min_length=1, max_length=_MAX_FILENAME_LEN)
    # data:{mediaType};base64,{...} — text/code files carry the UTF-8 text
    # encoded as base64; PDF/DOCX carry the raw file bytes (OpenRouter file API).
    file_data: str = Field(min_length=1, max_length=_MAX_FILE_DATA_LEN)


class FileContentPart(BaseModel):
    type: Literal["file"]
    file: FilePayload


# Multimodal message content (OpenRouter / OpenAI vision + file format). A plain
# string stays the text-only path; an array carries a text part (first) plus
# image parts and/or file parts.
ContentPart = Annotated[
    Union[TextContentPart, ImageContentPart, FileContentPart],
    Field(discriminator="type"),
]


class SendMessageRequest(BaseModel):
    content: str | list[ContentPart]
    model: str = Field(min_length=1, max_length=128)
    # Client-generated idempotency key (tr-n5b): one per send ATTEMPT. A retried /
    # replayed / multi-tab duplicate of the SAME logical send reuses the same id,
    # letting the backend dedup gate reject it BEFORE persisting/reserving/charging
    # (defense-in-depth behind the frontend submit lock). None ⇒ older client ⇒ no
    # dedup, full backward compatibility.
    client_request_id: UUID | None = None
    web_search_enabled: bool = False
    # Extended thinking (reasoning). Honored only when the model advertises
    # supports_reasoning; effort maps to a token budget in the billing pipeline.
    thinking_enabled: bool = False
    thinking_effort: Literal["low", "medium", "high"] = "medium"
    # Agentic tool-use (tr-cy0.2). Honored only when the model advertises
    # supports_tools; opt-in because the loop costs real multi-round money.
    tools_enabled: bool = False
    # IDs of persisted history images the user toggled OUT of context. The
    # backend drops these when re-injecting images into the conversation.
    excluded_image_ids: list[str] = Field(default_factory=list)
    # IDs of persisted history files the user toggled OUT of context. Dropped
    # when re-injecting files into the conversation (parity with images).
    excluded_file_ids: list[str] = Field(default_factory=list)

    @field_validator("content")
    @classmethod
    def _validate_content(
        cls, v: "str | list[ContentPart]"
    ) -> "str | list[ContentPart]":
        # PydanticCustomError (not ValueError) per project convention — keeps the
        # 422 payload shape consistent with the rest of the schemas.
        if isinstance(v, str):
            if not 1 <= len(v) <= _MAX_TEXT_LEN:
                raise PydanticCustomError(
                    "content_length",
                    "Сообщение должно содержать от 1 до {max} символов",
                    {"max": _MAX_TEXT_LEN},
                )
            return v
        if not v:
            raise PydanticCustomError(
                "content_empty", "Сообщение не может быть пустым"
            )
        images = sum(1 for p in v if isinstance(p, ImageContentPart))
        if images > _MAX_IMAGES:
            raise PydanticCustomError(
                "too_many_images",
                "Не более {max} изображений на сообщение",
                {"max": _MAX_IMAGES},
            )
        attachments = sum(
            1 for p in v if isinstance(p, (ImageContentPart, FileContentPart))
        )
        if attachments > _MAX_ATTACHMENTS:
            raise PydanticCustomError(
                "too_many_attachments",
                "Не более {max} вложений на сообщение",
                {"max": _MAX_ATTACHMENTS},
            )
        return v


class EditMessageRequest(BaseModel):
    # Text-only edit (design §2). Attachments on the original message are carried
    # over server-side; the client never re-sends them.
    content: str = Field(min_length=1, max_length=_MAX_TEXT_LEN)
    # Live model picker (tr-77u). None ⇒ inherit the conversation's current model;
    # a non-None value (the model selected in the picker at edit time) overrides it
    # for this forced reply AND is persisted to conversation.model_id, mirroring
    # send/regenerate so the answer is priced/recorded against the chosen model.
    model: str | None = Field(default=None, min_length=1, max_length=128)
    # Live composer toggles for the forced reply (tr-1hm). Each defaults to None ⇒
    # inherit the replaced reply's posture (thinking/web) or off (tools); a non-None
    # value overrides it, so editing in a tool/thinking/web chat keeps that mode.
    thinking_enabled: bool | None = None
    thinking_effort: Literal["low", "medium", "high"] | None = None
    web_search_enabled: bool | None = None
    tools_enabled: bool | None = None


class RegenerateRequest(BaseModel):
    """Optional per-regeneration overrides for a NEW assistant version.

    Every field defaults to None ⇒ inherit the conversation's current model and
    the original turn's thinking / web-search posture. A non-None value overrides
    just that dimension for this one regeneration; the new version records its own
    model + cost (design §2).
    """

    model: str | None = Field(default=None, min_length=1, max_length=128)
    thinking_enabled: bool | None = None
    thinking_effort: Literal["low", "medium", "high"] | None = None
    web_search_enabled: bool | None = None
    # Live «Инструменты» toggle (tr-1hm). None ⇒ off (no surprise multi-round
    # spend); we can't reliably detect prior tool use from the persisted reply.
    tools_enabled: bool | None = None


class ActivePathSwitchRequest(BaseModel):
    # The sibling version to make active. Switching deactivates the currently
    # active branch at this node's fork, activates this node, and walks its
    # subtree to a leaf — no model call, no charge (the free version switcher).
    node_id: UUID


class ThinkingSegmentDTO(BaseModel):
    """A persisted reasoning segment for reload / post-Stop reconcile: stable id,
    its thinking text, and the one-line title (None for an untitled segment).
    List order is the render order."""

    id: str
    text: str
    summary: str | None = None


class ToolDiffDTO(BaseModel):
    added: int = Field(ge=0)
    removed: int = Field(ge=0)


class ToolStepDTO(BaseModel):
    """A persisted agentic tool step for reload / post-Stop reconcile (tr-cy0.2.7).
    List order is render order; an interrupted step persists as `error`."""

    id: str
    tool: str
    title: str
    filename: str | None = None
    diff: ToolDiffDTO | None = None
    presented: bool = False
    status: Literal["running", "done", "error"]
    output: str | None = None
    # Code-execution metadata (run_python, epic tr-s3y1 Phase 2). Defaults keep
    # file-tool steps unchanged.
    stderr: str | None = None
    duration_ms: int | None = None
    truncated: bool = False
    # Submitted code (raw tool args) for run_python, persisted so the run-card shows
    # the code after reload (tr-jk4j). None for file tools.
    input: str | None = None
    # Rich outputs persisted for reload (Phase 4 tr-5ktl): image/png entries carry an
    # artifact_id (the base64 bytes live in the artifact, not here); text/html and
    # text/plain are kept inline. None for file tools / no rich output.
    results: list | None = None


class ArtifactResponse(BaseModel):
    """A model-authored file's content for the artifact card / download / canvas
    (tr-cy0.1.10 / 2.13). Keyed by (conversation, filename). By default the LATEST
    version is returned; the optional `?version=N` query param selects an older
    one (tr-qil). `version` is the returned row's version; `version_count` is how
    many versions exist for that file's group — the canvas shows a <n/m> switcher
    when it is > 1."""

    filename: str
    media_type: str
    content: str
    version: int
    version_count: int


class MessageResponse(BaseModel):
    id: UUID
    role: str
    content: str
    model_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None = None
    cost_micro_rub: int | None
    request_id: UUID | None
    # Extended-thinking text, so a reload can restore the collapsible block.
    thinking: str | None = None
    # Per-segment reasoning (multi-segment timeline). NULL for legacy/single-
    # segment rows, which degrade to one segment built from `thinking`.
    thinking_segments: list[ThinkingSegmentDTO] | None = None
    # Agentic tool steps for the timeline (tr-cy0.2.7); NULL when no tools ran.
    tool_steps: list[ToolStepDTO] | None = None
    created_at: datetime
    # ── Version history (migration 014) ──────────────────────────────────
    # Self-FK to the message this node replaces/answers; NULL for the first
    # user turn and for legacy (pre-014) rows.
    parent_id: UUID | None = None
    # 1-based ordinal among siblings (same parent_id + role) by (created_at, id).
    version_index: int = 1
    # Sibling count for this turn; 1 ⇒ no switcher.
    version_count: int = 1
    # Whether this node is on the rendered root→leaf path.
    is_active: bool = True
    # Whether this assistant turn was generated with web-search enabled, so the
    # meta-row globe survives a reload. Recovered from the usage_log keyed by
    # request_id (no per-message column); always False for user turns.
    web_search: bool = False
    # Whether this assistant turn was STOPPED mid-reply (vs completed normally), so
    # a reloaded partial reply shows «Остановлено» instead of «Готово». Derived on
    # the read path from usage_logs.status == 'partial' keyed by request_id (no
    # per-message column, no finalize change); always False for user turns and for
    # non-partial statuses (completed / failed / refunded / active).
    stopped: bool = False


class BranchTotals(BaseModel):
    """Conversation-wide aggregate over ALL messages, including inactive
    siblings — the "what has this whole chat cost across every experiment"
    figure for the «Все ветки» analytics section (Task 13)."""

    total_cost_micro_rub: int
    total_input_tokens: int
    total_output_tokens: int
    total_reasoning_tokens: int
    message_count: int
    generation_count: int


class RequestResultResponse(BaseModel):
    """The persisted result of a chat request, shaped like the message_done SSE
    event so the client can reuse applyMessageDone after a Stop (tr-sgz)."""

    request_id: UUID
    message_id: UUID
    user_message_id: UUID | None = None
    active_leaf_id: UUID | None = None
    actual_cost: str
    balance_after: str
    tokens_used: dict[str, int]
    # Per-segment reasoning so post-Stop reconcile restores the titled timeline
    # without a full reload (tr-cy0.3.7). NULL for single-segment/legacy replies.
    thinking_segments: list[ThinkingSegmentDTO] | None = None
    # Agentic tool steps for post-Stop reconcile (tr-cy0.2.7).
    tool_steps: list[ToolStepDTO] | None = None
    # Whether this reply was stopped mid-stream (usage_logs.status == 'partial'),
    # so the post-Stop reconcile can flip the live bubble's terminal node to
    # «Остановлено» without waiting for a reload (tr-fo1).
    stopped: bool = False


class ConversationResponse(BaseModel):
    id: UUID
    title: str | None
    model_id: str
    created_at: datetime
    updated_at: datetime
    # Last-used generation posture (migration 020), so reopening a chat restores
    # the model + toggles of its last request. NULL ⇒ frontend keeps its defaults.
    last_thinking_enabled: bool | None = None
    last_thinking_effort: Literal["low", "medium", "high"] | None = None
    last_tools_enabled: bool | None = None
    last_web_search_enabled: bool | None = None
    # Populated only by GET /conversations/{id}. The other routes set
    # response_model_exclude_none=True so this field is dropped from their output.
    messages: list[MessageResponse] | None = None
    # All-branch aggregate; populated only by GET /conversations/{id} (Task 13).
    branch_totals: BranchTotals | None = None


class ConversationListResponse(BaseModel):
    items: list[ConversationResponse]
    total: int
    page: int
    per_page: int


# ── Extended-thinking SSE event data (tr-cy0.3) ──────────────────────────────
# A one-line segment title; the reference headers are a single short sentence.
_MAX_SUMMARY_LEN = 240
# Stable per-stream segment identifier (e.g. "seg-0"); short by construction.
_MAX_SEGMENT_ID_LEN = 64


class ThinkingSegmentEvent(BaseModel):
    """`thinking_segment` SSE data — opens a new reasoning segment.

    The `thinking_delta` frames that follow belong to this segment until the
    next `thinking_segment`. A stream that emits only `thinking_delta` (no
    `thinking_segment`) is the LEGACY single-segment shape: the client treats it
    as one segment at index 0 and the current UI is unchanged (backward
    compatible). `index` is the 0-based ordinal; `segment_id` is the stable id
    `thinking_summary` references later.
    """

    segment_id: str = Field(min_length=1, max_length=_MAX_SEGMENT_ID_LEN)
    index: int = Field(ge=0)


class ThinkingSummaryEvent(BaseModel):
    """`thinking_summary` SSE data — delivers/updates a segment's one-line title.

    The title is a Russian heuristic formed from the segment text (spike
    tr-cy0.3.1: the provider gives no per-segment heading), so it MAY arrive
    AFTER that segment's `thinking_delta` text. Idempotent by `segment_id`: a
    repeat for the same id overwrites the previous title.
    """

    segment_id: str = Field(min_length=1, max_length=_MAX_SEGMENT_ID_LEN)
    summary: str = Field(min_length=1, max_length=_MAX_SUMMARY_LEN)


# ── Agentic tool-step SSE event data (tr-cy0.2.6) ────────────────────────────
_MAX_STEP_ID_LEN = 32


class ToolCallStartEvent(BaseModel):
    """`tool_call_start` — a tool step begins (live pulsing dot). `step_id` is
    `tc-{round}-{index}`; `title` is a RU label, `tool` the function name."""

    step_id: str = Field(min_length=1, max_length=_MAX_STEP_ID_LEN)
    tool: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=_MAX_SUMMARY_LEN)


class ToolCallArgumentsDeltaEvent(BaseModel):
    """`tool_call_arguments_delta` — the tool's argument JSON streams in (the user
    watches the filename + content form, like Claude)."""

    step_id: str = Field(min_length=1, max_length=_MAX_STEP_ID_LEN)
    delta: str


class ToolResultEvent(BaseModel):
    """`tool_result` — a step resolves: ✓ done or red error, carrying the filename
    chip, inline diff (+added/−removed) and the «presented» marker."""

    step_id: str = Field(min_length=1, max_length=_MAX_STEP_ID_LEN)
    status: Literal["done", "error"]
    filename: str | None = None
    diff: ToolDiffDTO | None = None
    presented: bool = False
    output: str | None = None
    # Code-execution metadata (run_python, epic tr-s3y1 Phase 2).
    stderr: str | None = None
    duration_ms: int | None = None
    truncated: bool = False
    # Rich outputs (Phase 4 tr-5ktl): live tagged results (image/png base64, text/html,
    # text/plain) rendered inline in the run-card. Images also persist as artifacts.
    results: list = Field(default_factory=list)


class ToolLoopHaltedEvent(BaseModel):
    """`tool_loop_halted` — a hard bound was hit (architecture §3): the loop
    stopped and billing finalized on what was spent."""

    reason: str = Field(min_length=1, max_length=64)
