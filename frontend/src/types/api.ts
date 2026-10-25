export type UserRole = "user" | "admin";
export type UserStatus = "active" | "blocked";

export interface User {
  id: string;
  email: string;
  display_name: string;
  full_name?: string | null;
  role: UserRole;
  status: UserStatus;
  balance: string;
  // tr-tl3q: held funds (active reservations) + spendable balance, so the UI can
  // explain why "available" can be below the headline balance. Optional: the
  // login payload predates these on cached/older responses.
  reserved?: string;
  available?: string;
  has_password?: boolean;
  has_yandex?: boolean;
  scheduled_delete_at?: string | null;
  created_at?: string;
}

export interface SessionItem {
  id: string;
  ip_address: string | null;
  user_agent: string | null;
  created_at: string;
  expires_at: string;
  is_current: boolean;
}

export interface SessionListResponse {
  items: SessionItem[];
}

export interface ModelUsageBreakdown {
  model_id: string;
  messages: number;
  input_tokens: number;
  output_tokens: number;
  spent: string;
}

export interface AnalyticsResponse {
  total_messages: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_spent: string;
  by_model: ModelUsageBreakdown[];
}

export interface AuthResponse {
  access_token: string;
  token_type: string;
  user: User;
}

export type MessageRole = "user" | "assistant" | "system";

// Sums over ALL conversation messages including inactive siblings/branches —
// the "what has this whole chat cost across every version" figure surfaced in
// the «Все ветки» analytics section. Computed server-side (branching_service).
export interface BranchTotals {
  total_cost_micro_rub: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_reasoning_tokens: number;
  message_count: number;
  generation_count: number;
}

// A persisted reasoning segment (tr-cy0.3.7): stable id, its thinking text, and
// the one-line title (null for an untitled segment). Restored on reload / after
// a Stop to rebuild the multi-segment thinking timeline. List order is render
// order.
export interface ThinkingSegmentDTO {
  id: string;
  text: string;
  summary?: string | null;
}

export interface MessageResponse {
  id: string;
  role: MessageRole;
  content: string;
  model_id: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  reasoning_tokens?: number | null;
  cost_micro_rub: number | null;
  request_id?: string | null;
  // Persisted extended-thinking text, restored into the collapsible block.
  thinking?: string | null;
  // Per-segment reasoning for the multi-segment timeline; null for legacy /
  // single-segment rows, which degrade to one segment built from `thinking`.
  thinking_segments?: ThinkingSegmentDTO[] | null;
  // Persisted agentic tool steps (tr-cy0.2); null when no tools ran.
  tool_steps?: ToolStepDTO[] | null;
  // Version history (migration 014). parent_id links a node to the turn it
  // replaces/answers; siblings sharing (parent_id, role) are versions of one
  // turn. version_index is the 1-based ordinal among siblings, version_count
  // the sibling count (1 => no switcher). is_active marks the rendered path.
  parent_id?: string | null;
  version_index?: number;
  version_count?: number;
  is_active?: boolean;
  // Whether this assistant turn was generated with web-search on, recovered from
  // the usage_log server-side so the meta-row globe survives a reload.
  web_search?: boolean;
  // Whether this assistant turn was STOPPED mid-reply (usage_logs.status ==
  // 'partial'), recovered server-side so a reloaded stopped reply shows
  // «Остановлено» instead of «Готово» (tr-fo1). False for completed replies.
  stopped?: boolean;
  created_at: string;
}

export interface ConversationResponse {
  id: string;
  title: string | null;
  model_id: string;
  created_at: string;
  updated_at: string;
  // Last-used generation posture (migration 020), restored when reopening the
  // chat. null ⇒ never recorded → keep current/default toggles.
  last_thinking_enabled?: boolean | null;
  last_thinking_effort?: "low" | "medium" | "high" | null;
  last_tools_enabled?: boolean | null;
  last_web_search_enabled?: boolean | null;
  messages?: MessageResponse[];
  // All-branch aggregate (incl. inactive siblings) for the «Все ветки»
  // analytics section. Absent on legacy/un-branched payloads.
  branch_totals?: BranchTotals;
}

export interface ConversationListResponse {
  items: ConversationResponse[];
  total: number;
  page: number;
  per_page: number;
}

export interface ModelInfo {
  id: string;
  name: string;
  provider: string;
  input_price_per_m: string;
  output_price_per_m: string;
  context_window: number;
  status: string;
  supports_image_input: boolean;
  supports_file_input: boolean;
  supports_tools: boolean;
  supports_reasoning: boolean;
  supports_web_search: boolean;
  last_updated?: string;
}

export interface ModelListResponse {
  models: ModelInfo[];
}

export interface MessageContentTextPart {
  type: "text";
  text: string;
}

export interface MessageContentImagePart {
  type: "image_url";
  image_url: { url: string };
}

// OpenRouter file part. `file_data` is a `data:<mime>;base64,…` URI: text/code
// files carry their UTF-8 text base64-encoded, PDF/DOCX carry the raw bytes.
export interface MessageContentFilePart {
  type: "file";
  file: { filename: string; file_data: string };
}

// OpenRouter / OpenAI multimodal content. Text-only messages keep the plain
// string form; attachment messages send an array (text first, then image/file
// parts).
export type MessageContentPart =
  | MessageContentTextPart
  | MessageContentImagePart
  | MessageContentFilePart;

export type ThinkingEffort = "low" | "medium" | "high";

export interface SendMessageRequest {
  content: string | MessageContentPart[];
  model: string;
  // Client-generated idempotency key (tr-n5b): one UUID per send ATTEMPT so the
  // backend dedup gate rejects a multi-tab / retry / programmatic duplicate of
  // the SAME logical send before reserving/charging. Omitted ⇒ no dedup.
  client_request_id?: string;
  web_search_enabled?: boolean;
  // Extended thinking: enable + effort level (mapped to a token budget server-side).
  thinking_enabled?: boolean;
  thinking_effort?: ThinkingEffort;
  // Agentic tool-use (tr-cy0.2). Honored only on a supports_tools model.
  tools_enabled?: boolean;
  // IDs of persisted history images the user toggled out of context.
  excluded_image_ids?: string[];
  // IDs of persisted history files the user toggled out of context.
  excluded_file_ids?: string[];
}

// POST …/messages/{id}/regenerate — every field optional; an omitted field
// (never sent as null) tells the backend to reuse the original turn's setting.
export interface RegenerateRequest {
  model?: string;
  thinking_enabled?: boolean;
  thinking_effort?: ThinkingEffort;
  web_search_enabled?: boolean;
  // Live «Инструменты» toggle for the new version (tr-1hm).
  tools_enabled?: boolean;
}

// POST …/messages/{id}/edit — text-only edit-and-resend; attachments carry over
// server-side from the original user turn. The live composer toggles ride along
// so the forced reply keeps the chat's tool/thinking/web mode (tr-1hm).
export interface EditMessageRequest {
  content: string;
  // Live model picker (tr-77u): the model selected at edit time. Omitted ⇒ the
  // backend reuses the conversation's current model.
  model?: string;
  thinking_enabled?: boolean;
  thinking_effort?: ThinkingEffort;
  web_search_enabled?: boolean;
  tools_enabled?: boolean;
}

// ── Image persistence / gallery / analytics ────────────────────────────────

// A persisted image as held in the per-conversation context map. The backend
// returns id/message_id/full_base64/media_type/created_at + included_in_context;
// the loader maps the snake-case flag onto includedInContext, which the per-image
// checkbox toggles and persists back via PATCH (tr-kx4f).
export interface ContextImage {
  id: string;
  message_id: string | null;
  full_base64: string;
  media_type: string;
  created_at: string;
  includedInContext: boolean;
}

export interface GalleryImage {
  id: string;
  thumbnail_base64: string;
  media_type: string;
  created_at: string;
  conversation_id: string | null;
  file_size_bytes: number;
  /** True when the owning conversation is archived; rendered dimmed in the gallery. */
  archived: boolean;
}

export interface GalleryResponse {
  items: GalleryImage[];
  total: number;
  page: number;
  has_more: boolean;
}

// ── File persistence / Files view ───────────────────────────────────────────

export type FileKind = "text" | "pdf" | "docx";

// A persisted file as held in the per-conversation context map. The backend
// returns metadata only (no payload) + included_in_context; the loader maps the
// snake-case flag onto includedInContext, toggled+persisted via PATCH, mirroring
// ContextImage (tr-kx4f).
export interface ContextFile {
  id: string;
  message_id: string | null;
  filename: string;
  media_type: string;
  kind: FileKind;
  file_size_bytes: number;
  token_estimate: number;
  created_at: string;
  includedInContext: boolean;
}

export interface GalleryFile {
  id: string;
  filename: string;
  media_type: string;
  kind: FileKind;
  file_size_bytes: number;
  token_estimate: number;
  created_at: string;
  conversation_id: string | null;
  /** True when the owning conversation is archived; rendered dimmed. */
  archived: boolean;
}

export interface GalleryFilesResponse {
  items: GalleryFile[];
  total: number;
  page: number;
  has_more: boolean;
}

// An AI-authored artifact in the gallery's opt-in "Созданные ИИ" mode: the LATEST
// version of a logical file (distinct group_id). Downloaded via the existing
// per-conversation artifact endpoint, keyed by (conversation_id, filename).
export interface GalleryArtifact {
  group_id: string;
  filename: string;
  media_type: string;
  size_bytes: number;
  version: number;
  version_count: number;
  conversation_id: string | null;
  created_at: string;
}

export interface GalleryArtifactsResponse {
  items: GalleryArtifact[];
  total: number;
  page: number;
  has_more: boolean;
}

// Single-file payload, fetched lazily when the user opens/downloads a file.
export interface FileDetail {
  id: string;
  filename: string;
  media_type: string;
  kind: FileKind;
  base64_data: string;
}

export interface StorageStats {
  text_size_bytes: number;
  images_size_bytes: number;
  files_size_bytes: number;
  total_images_count: number;
  total_files_count: number;
  total_conversations_count: number;
  total_messages_count: number;
}

export interface ApiError {
  detail: string;
  code?: string;
}

export type TransactionType = "debit" | "credit" | "adjustment" | "refund";

export interface TransactionItem {
  id: string;
  type: TransactionType;
  amount: string;
  balance_after: string;
  description: string;
  created_at: string;
  request_id?: string;
  payment_id?: string;
}

export interface TransactionListResponse {
  items: TransactionItem[];
  total: number;
  page: number;
  per_page: number;
  pages: number;
}

export interface PaymentCreateResponse {
  payment_id: string;
  confirmation_url: string;
  status: string;
  amount: string;
}

export interface Notification {
  id: string;
  type: string;
  title: string;
  message: string;
  is_read: boolean;
  created_at: string;
  data?: Record<string, unknown>;
}

export interface NotificationListResponse {
  items: Notification[];
  total: number;
  page: number;
  per_page: number;
}

export interface SSEMessageStart {
  request_id: string;
  estimated_cost: string;
}

export interface SSEContentDelta {
  delta: string;
}

// Extended-thinking text streamed ahead of (and separately from) the answer.
export interface SSEThinkingDelta {
  delta: string;
}

// Opens a new reasoning segment (tr-cy0.3): the `thinking_delta` frames that
// follow belong to it until the next `thinking_segment`. A stream that emits
// only `thinking_delta` (no `thinking_segment`) is the LEGACY single-segment
// shape — the client treats it as one segment at index 0 and the UI is
// unchanged. `index` is the 0-based ordinal; `segment_id` is the stable id the
// matching `thinking_summary` references.
export interface SSEThinkingSegment {
  segment_id: string;
  index: number;
}

// Delivers/updates a reasoning segment's one-line title (Russian heuristic —
// spike tr-cy0.3.1). MAY arrive AFTER that segment's `thinking_delta` text, and
// is idempotent by `segment_id`: a repeat overwrites the previous title.
export interface SSEThinkingSummary {
  segment_id: string;
  summary: string;
}

// ── Agentic tool steps (tr-cy0.2) ───────────────────────────────────────────
export interface ToolDiffDTO {
  added: number;
  removed: number;
}

// A tool step begins (live pulsing dot). step_id = `tc-{round}-{index}`.
export interface SSEToolCallStart {
  step_id: string;
  tool: string;
  title: string;
}

// The tool's argument JSON streams in (filename + content form live, like Claude).
export interface SSEToolCallArgumentsDelta {
  step_id: string;
  delta: string;
}

// A tool step resolves: ✓ done or red error, carrying the filename chip, inline
// diff (+added/−removed) and the «Файл представлен» marker.
export interface SSEToolResult {
  step_id: string;
  status: "done" | "error";
  filename?: string | null;
  diff?: ToolDiffDTO | null;
  presented?: boolean;
  output?: string | null;
  // Code-execution metadata (run_python, epic tr-s3y1 Phase 2).
  stderr?: string | null;
  duration_ms?: number | null;
  truncated?: boolean;
  // Rich outputs (Phase 4 tr-5ktl): live tagged results — image/png (base64 data),
  // text/html, text/plain — rendered inline in the run-card.
  results?: RichResultDTO[] | null;
}

// One rich result. Live carries base64 `data` for image/png; persisted (ToolStepDTO)
// carries an artifact_id/filename instead so the image is re-fetched on reload.
export interface RichResultDTO {
  type: "image/png" | "text/html" | "text/plain" | "file";
  data?: string;
  artifact_id?: string;
  filename?: string;
  // `file` (Phase 5, tr-an0g): generated workspace file download chip metadata.
  media_type?: string;
  size?: number;
  unavailable?: boolean;
}

// A hard loop bound was hit (architecture §3); the loop stopped and finalized.
export interface SSEToolLoopHalted {
  reason: string;
}

// A persisted tool step (reload / post-Stop reconcile). List order = render order.
export interface ToolStepDTO {
  id: string;
  tool: string;
  title: string;
  filename?: string | null;
  diff?: ToolDiffDTO | null;
  presented?: boolean;
  status: "running" | "done" | "error";
  output?: string | null;
  // Code-execution metadata (run_python, epic tr-s3y1 Phase 2).
  stderr?: string | null;
  duration_ms?: number | null;
  truncated?: boolean;
  // Submitted code (raw tool args) for run_python, for the run-card on reload (tr-jk4j).
  input?: string | null;
  // Rich outputs persisted for reload (Phase 4 tr-5ktl): image/png entries carry an
  // artifact_id + filename (re-fetched), text/html|text/plain carry inline data.
  results?: RichResultDTO[] | null;
}

export interface SSEMessageDone {
  request_id: string;
  actual_cost: string;
  balance_after: string;
  tokens_used: { input: number; output: number; reasoning?: number };
  // DB id of the just-persisted assistant node + the conversation's new active
  // leaf, so the client swaps its local genId() placeholder for the real id and
  // updates the active-path pointer (replaces the happy-path re-fetch).
  message_id?: string;
  active_leaf_id?: string | null;
  // DB id of this turn's user node (the assistant's parent). The send path keeps
  // the user bubble's genId() placeholder, so the client swaps it here too —
  // otherwise a subsequent edit/resend targets a non-existent id (404).
  user_message_id?: string | null;
  // Per-segment reasoning — present only on the post-Stop result endpoint (which
  // reuses this shape), absent on the live message_done SSE event (segments
  // arrive there via thinking_segment/thinking_summary). Lets a Stop reconcile
  // restore the titled timeline (tr-cy0.3.7/3.8).
  thinking_segments?: ThinkingSegmentDTO[] | null;
  // Same idea for agentic tool steps on the post-Stop result endpoint (tr-cy0.2.7).
  tool_steps?: ToolStepDTO[] | null;
  // Whether this reply was stopped mid-stream (usage_logs.status == 'partial'),
  // present on the post-Stop result endpoint so the live bubble flips its terminal
  // node to «Остановлено» without a reload (tr-fo1). Absent on the live SSE event.
  stopped?: boolean;
}

export interface SSEError {
  code: string;
  message: string;
  required?: string;
  available?: string;
}

export type SSEEventPayload =
  | { event: "message_start"; data: SSEMessageStart }
  | { event: "content_delta"; data: SSEContentDelta }
  | { event: "thinking_delta"; data: SSEThinkingDelta }
  | { event: "thinking_segment"; data: SSEThinkingSegment }
  | { event: "thinking_summary"; data: SSEThinkingSummary }
  | { event: "tool_call_start"; data: SSEToolCallStart }
  | { event: "tool_call_arguments_delta"; data: SSEToolCallArgumentsDelta }
  | { event: "tool_result"; data: SSEToolResult }
  | { event: "tool_loop_halted"; data: SSEToolLoopHalted }
  | { event: "message_done"; data: SSEMessageDone }
  | { event: "error"; data: SSEError }
  | { event: string; data: unknown };
