import type {
  EditMessageRequest,
  MessageContentPart,
  RegenerateRequest,
  SendMessageRequest,
  SSEEventPayload,
  ThinkingEffort,
} from "@/types/api";
import { parseErrorBody } from "@/lib/errors";

const DEFAULT_BASE_URL = "http://localhost:8000";

export interface StreamChatOptions {
  baseUrl?: string;
  signal?: AbortSignal;
  // Client-generated idempotency key (tr-n5b): a fresh UUID per send attempt,
  // forwarded as `client_request_id` so a duplicate of the SAME logical send is
  // deduped server-side. Omitted ⇒ the backend skips the dedup gate.
  clientRequestId?: string;
  webSearchEnabled?: boolean;
  thinkingEnabled?: boolean;
  thinkingEffort?: ThinkingEffort;
  toolsEnabled?: boolean;
  excludedImageIds?: string[];
  excludedFileIds?: string[];
}

// Override knobs shared by regenerate (send-shaped settings); the URL + which
// fields are sent differ per action. Omitted fields are dropped from the body
// (not sent null) so the backend reuses the original turn's setting.
export interface StreamActionOptions {
  baseUrl?: string;
  signal?: AbortSignal;
  modelId?: string;
  webSearchEnabled?: boolean;
  thinkingEnabled?: boolean;
  thinkingEffort?: ThinkingEffort;
  toolsEnabled?: boolean;
}

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const target = `${name}=`;
  for (const raw of document.cookie.split(";")) {
    const c = raw.trim();
    if (c.startsWith(target)) return decodeURIComponent(c.slice(target.length));
  }
  return null;
}

function resolveBaseUrl(baseUrl?: string): string {
  return baseUrl ?? process.env.NEXT_PUBLIC_API_URL ?? DEFAULT_BASE_URL;
}

// Shared POST→SSE generator. All three public helpers funnel through here so
// CSRF forwarding, fetch error handling, and frame parsing live in ONE place;
// only the URL + body differ between send / regenerate / edit.
async function* streamSse(
  url: string,
  body: unknown,
  token: string,
  signal?: AbortSignal,
): AsyncGenerator<SSEEventPayload> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
    Authorization: `Bearer ${token}`,
  };
  // CSRFMiddleware requires X-CSRF-Token on every mutating method (POST too).
  // The cookie is set by any prior GET; we forward it as a header here so the
  // SSE POST isn't rejected with 403 before the stream even begins.
  const csrf = readCookie("csrf_token");
  if (csrf) headers["X-CSRF-Token"] = csrf;

  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers,
      credentials: "include",
      body: JSON.stringify(body),
      signal,
    });
  } catch {
    // Intentional abort (stop button / new chat) — end silently, not an error.
    if (signal?.aborted) return;
    yield { event: "error", data: { code: "network", message: "Ошибка сети" } };
    return;
  }

  if (!response.ok || response.body === null) {
    const text = await response.text().catch(() => "");
    // Parse the backend error envelope into a clean localized message + code —
    // NEVER yield the raw body. For a 422 it carries details[].msg plus the
    // echoed user `input` (~99k chars); parseErrorBody keeps only the msg so the
    // error panel shows human text, not raw JSON (tr-5zt).
    const { message, code } = parseErrorBody(text, response.status);
    // The backend idempotency gate (tr-n5b) rejects a duplicate of the SAME
    // logical send with 409 `duplicate_request`. The frontend submit lock already
    // prevents the common case, so a 409 here is a benign "already in flight/sent":
    // end the stream silently with NO error event, so the error UI shows nothing
    // scary and no duplicate turn is rendered.
    if (response.status === 409 && code === "duplicate_request") {
      return;
    }
    // Emit "http_error" (deliberately NOT a key in ERROR_MESSAGES) so the
    // consumer's getErrorMessage(code, message) falls through to our
    // already-localized `message` (e.g. the 32000-char details[].msg) instead of
    // collapsing a real code like "validation_error" back to the generic
    // envelope text. The specific message is what the user needs to see (tr-5zt).
    yield {
      event: "error",
      data: { code: "http_error", message },
    };
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let sep = buffer.indexOf("\n\n");
      while (sep !== -1) {
        const rawEvent = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const parsed = parseSseFrame(rawEvent);
        if (parsed) yield parsed;
        sep = buffer.indexOf("\n\n");
      }
    }

    if (buffer.trim().length > 0) {
      const parsed = parseSseFrame(buffer);
      if (parsed) yield parsed;
    }
  } catch {
    // Reader rejects with an AbortError when the caller aborts mid-stream.
    if (signal?.aborted) return;
    yield { event: "error", data: { code: "network", message: "Ошибка сети" } };
  }
}

export async function* streamChatMessage(
  conversationId: string,
  content: string | MessageContentPart[],
  model: string,
  token: string,
  options: StreamChatOptions = {},
): AsyncGenerator<SSEEventPayload> {
  const url = `${resolveBaseUrl(
    options.baseUrl,
  )}/api/v1/chat/conversations/${conversationId}/messages`;
  const body: SendMessageRequest & { stream: boolean } = {
    content,
    model,
    stream: true,
    web_search_enabled: options.webSearchEnabled ?? false,
    thinking_enabled: options.thinkingEnabled ?? false,
    thinking_effort: options.thinkingEffort ?? "medium",
    tools_enabled: options.toolsEnabled ?? false,
    excluded_image_ids: options.excludedImageIds ?? [],
    excluded_file_ids: options.excludedFileIds ?? [],
  };
  // Idempotency key (tr-n5b): only sent when provided, so an older caller stays
  // byte-identical and the backend keeps full backward compatibility.
  if (options.clientRequestId !== undefined) {
    body.client_request_id = options.clientRequestId;
  }
  yield* streamSse(url, body, token, options.signal);
}

// Regenerate an existing assistant node as a new sibling version. Override
// fields are omitted (not sent null) when undefined so the backend reuses the
// original turn's model/thinking/web-search settings.
export async function* streamRegenerate(
  conversationId: string,
  assistantMessageId: string,
  token: string,
  options: StreamActionOptions = {},
): AsyncGenerator<SSEEventPayload> {
  const url = `${resolveBaseUrl(
    options.baseUrl,
  )}/api/v1/chat/conversations/${conversationId}/messages/${assistantMessageId}/regenerate`;
  const body: RegenerateRequest = {};
  if (options.modelId !== undefined) body.model = options.modelId;
  if (options.thinkingEnabled !== undefined)
    body.thinking_enabled = options.thinkingEnabled;
  if (options.thinkingEffort !== undefined)
    body.thinking_effort = options.thinkingEffort;
  if (options.webSearchEnabled !== undefined)
    body.web_search_enabled = options.webSearchEnabled;
  if (options.toolsEnabled !== undefined)
    body.tools_enabled = options.toolsEnabled;
  yield* streamSse(url, body, token, options.signal);
}

// Edit-and-resend a user node (text only). Attachments carry over server-side
// from the original user turn. The live composer toggles ride along so the
// forced reply keeps the chat's tool/thinking/web mode (tr-1hm); the live model
// picker rides along too (tr-77u). Omitted fields are dropped from the body so
// the backend falls back to the inherited posture / conversation model.
export async function* streamEdit(
  conversationId: string,
  userMessageId: string,
  content: string,
  token: string,
  options: StreamActionOptions = {},
): AsyncGenerator<SSEEventPayload> {
  const url = `${resolveBaseUrl(
    options.baseUrl,
  )}/api/v1/chat/conversations/${conversationId}/messages/${userMessageId}/edit`;
  const body: EditMessageRequest = { content };
  if (options.modelId !== undefined) body.model = options.modelId;
  if (options.thinkingEnabled !== undefined)
    body.thinking_enabled = options.thinkingEnabled;
  if (options.thinkingEffort !== undefined)
    body.thinking_effort = options.thinkingEffort;
  if (options.webSearchEnabled !== undefined)
    body.web_search_enabled = options.webSearchEnabled;
  if (options.toolsEnabled !== undefined)
    body.tools_enabled = options.toolsEnabled;
  yield* streamSse(url, body, token, options.signal);
}

// Legacy alias preserved so callers don't break; new code should import
// getErrorMessage from "@/lib/errors" directly.
export { getErrorMessage as sseErrorMessage } from "@/lib/errors";

function parseSseFrame(frame: string): SSEEventPayload | null {
  let event = "message";
  const dataLines: string[] = [];

  for (const line of frame.split("\n")) {
    if (line.startsWith(":") || line.length === 0) continue;
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trim());
    }
  }

  if (dataLines.length === 0) return null;
  const raw = dataLines.join("\n");
  let data: unknown = raw;
  try {
    data = JSON.parse(raw);
  } catch {
    // leave as raw string
  }
  return { event, data } as SSEEventPayload;
}
