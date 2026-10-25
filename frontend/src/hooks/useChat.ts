"use client";

import { useCallback, useRef, useState } from "react";

import { ApiRequestError, apiClient } from "@/lib/api";
import { getAccessToken } from "@/lib/auth";
import { formatInsufficientFunds, getErrorMessage } from "@/lib/errors";
import { micro_to_cost_string } from "@/lib/microRub";
import { streamChatMessage, streamEdit, streamRegenerate } from "@/lib/sse";
import type {
  ContextFile,
  ContextImage,
  ConversationResponse,
  MessageContentPart,
  MessageResponse,
  SSEError,
  SSEEventPayload,
  SSEMessageDone,
  SSEMessageStart,
  ThinkingEffort,
  ToolStepDTO,
} from "@/types/api";
import type { ThinkingStep } from "@/types/thinking";
import {
  applyStreamEvent,
  coerceRunningStepsToError,
  createStreamBuffers,
  drainBuffers,
  snapshotBuffers,
} from "./streamAccumulator";
import type { LiveThinkingSegment, StreamEventHooks } from "./streamAccumulator";

// LiveThinkingSegment moved to streamAccumulator (the buffer's owner, tr-sja);
// re-exported here, its historical home, so existing imports keep working.
export type { LiveThinkingSegment };

// Raw shape returned by GET /api/v1/images/conversation/{id}: the backend sends
// the persisted toggle as snake-case included_in_context (tr-kx4f); the loader
// maps it onto the camelCase UI field includedInContext.
type ConversationImageDto = Omit<ContextImage, "includedInContext"> & {
  included_in_context: boolean;
};
// Raw shape returned by GET /api/v1/files/conversation/{id} (mirrors images).
type ConversationFileDto = Omit<ContextFile, "includedInContext"> & {
  included_in_context: boolean;
};

// Map persisted tool-step DTOs into the local ThinkingStep shape (tr-cy0.2.9).
// Undefined when absent/null so a message without tools carries no steps.
function mapToolSteps(
  steps: ToolStepDTO[] | null | undefined,
): ThinkingStep[] | undefined {
  return (
    steps?.map((s) => ({
      id: s.id,
      tool: s.tool,
      title: s.title,
      status: s.status,
      filename: s.filename ?? undefined,
      diff: s.diff ?? undefined,
      presented: s.presented ?? undefined,
      output: s.output ?? undefined,
      stderr: s.stderr ?? undefined,
      duration_ms: s.duration_ms ?? undefined,
      truncated: s.truncated ?? undefined,
      input: s.input ?? undefined,
      results: s.results ?? undefined,
    })) ?? undefined
  );
}

// Map a persisted message to the local ChatMessage shape. Mirrors the mapper in
// the /chat/[id] page so a post-send re-sync produces identical bubbles.
export function mapResponseToMessage(m: MessageResponse): ChatMessage {
  return {
    id: m.id,
    role: m.role === "user" ? "user" : "assistant",
    content: m.content,
    thinking: m.thinking ?? undefined,
    // Restore the multi-segment timeline; null/absent ⇒ undefined ⇒ the
    // single-segment fallback built from `thinking` (tr-cy0.3.8).
    thinkingSegments:
      m.thinking_segments?.map((s) => ({
        id: s.id,
        text: s.text,
        summary: s.summary ?? undefined,
      })) ?? undefined,
    // Restore agentic tool steps into the timeline (tr-cy0.2.9).
    thinkingSteps: mapToolSteps(m.tool_steps),
    reasoningTokens: m.reasoning_tokens ?? undefined,
    // Keep the request id so the copy-request-id control (and its tooltip)
    // render after a reload, not only while streaming.
    requestId: m.request_id ?? undefined,
    actualCost:
      m.cost_micro_rub != null
        ? micro_to_cost_string(m.cost_micro_rub)
        : undefined,
    tokensUsed:
      m.input_tokens != null && m.output_tokens != null
        ? { input: m.input_tokens, output: m.output_tokens }
        : undefined,
    // Version history (migration 014): kept on the bubble for the <n/m>
    // switcher + active-path tracking. undefined on legacy responses.
    parentId: m.parent_id ?? undefined,
    isActive: m.is_active,
    versionIndex: m.version_index,
    versionCount: m.version_count,
    modelId: m.model_id ?? undefined,
    // Restore the per-message web-search globe after a reload (it's otherwise
    // only set live during streaming).
    webSearchEnabled: m.web_search ?? undefined,
    // Restore the stopped/«Остановлено» state after a reload — derived server-side
    // from usage_logs.status == 'partial' (tr-fo1). Absent ⇒ completed reply.
    stopped: m.stopped ?? undefined,
    createdAt: m.created_at,
  };
}

// Finalize the streamed assistant bubble: the caller has already merged any
// drained delta text; here we swap the local genId placeholder for the real DB
// id (data.message_id) and stamp cost/tokens. Scoped to `placeholderId` ONLY —
// this is the streaming-vs-switch guard, so an in-flight stream never patches a
// bubble the user switched to mid-stream (every other row keeps its reference).
//
// `userPlaceholderId` (send path only): the send flow keeps the user bubble's
// genId() placeholder (it drops the post-stream re-fetch), so we also reconcile
// it to data.user_message_id here — otherwise a subsequent edit/resend of the
// just-sent message targets a non-existent id and 404s. Omitted by the
// regenerate/edit path (runActionStream), which reconciles ids via its own GET.
export function applyMessageDone(
  prev: ChatMessage[],
  placeholderId: string,
  data: SSEMessageDone,
  userPlaceholderId?: string,
): ChatMessage[] {
  return prev.map((m) => {
    if (m.id === placeholderId) {
      return {
        ...m,
        id: data.message_id ?? m.id,
        isStreaming: false,
        actualCost: data.actual_cost,
        tokensUsed: {
          input: data.tokens_used.input,
          output: data.tokens_used.output,
        },
        reasoningTokens: data.tokens_used.reasoning ?? m.reasoningTokens,
        // Post-Stop reconcile carries segments on the result DTO; the live
        // message_done omits them (already accumulated), so keep what's there.
        thinkingSegments: data.thinking_segments
          ? data.thinking_segments.map((s) => ({
              id: s.id,
              text: s.text,
              summary: s.summary ?? undefined,
            }))
          : m.thinkingSegments,
        // Same for tool steps (tr-cy0.2.9): restore from the result DTO post-Stop,
        // else keep the live-accumulated steps.
        thinkingSteps: data.tool_steps
          ? mapToolSteps(data.tool_steps)
          : m.thinkingSteps,
        // Post-Stop reconcile carries the partial/stopped flag so the live bubble
        // flips its terminal node to «Остановлено» without a reload (tr-fo1). The
        // live happy-path message_done omits it ⇒ a completed reply stays unset.
        stopped: data.stopped ?? m.stopped,
      };
    }
    if (
      userPlaceholderId &&
      m.id === userPlaceholderId &&
      data.user_message_id
    ) {
      return { ...m, id: data.user_message_id, localOnly: undefined };
    }
    return m;
  });
}

// Ids on the rendered active path (undefined isActive => active, covering legacy
// + freshly-streamed bubbles). Used to scope attachment loading so an inactive
// sibling's images/files never render on the wrong bubble.
export function activePathIds(messages: ChatMessage[]): string[] {
  return messages.filter((m) => m.isActive !== false).map((m) => m.id);
}

// Optimistic active-path re-derivation for the < n/m > switcher: swap the chosen
// sibling (from the lazily-loaded sibling cache) into the rendered transcript in
// place of whichever sibling of the same turn is currently shown. Returns the
// SAME array reference when `nodeId` is not a known sibling (nothing to swap),
// so callers can skip a no-op setMessages. The PUT response later reconciles
// the full server-authoritative path.
export function deriveActivePath(
  messages: ChatMessage[],
  siblings: MessageResponse[],
  nodeId: string,
): ChatMessage[] {
  const chosen = siblings.find((s) => s.id === nodeId);
  if (!chosen) return messages;
  const siblingIds = new Set(siblings.map((s) => s.id));
  let swapped = false;
  const next = messages.map((m) => {
    if (m.id === nodeId) {
      swapped = true;
      return { ...mapResponseToMessage(chosen), isActive: true };
    }
    if (siblingIds.has(m.id)) {
      swapped = true;
      return { ...mapResponseToMessage(chosen), isActive: true };
    }
    return m;
  });
  return swapped ? next : messages;
}

// Transient state for an image the user has attached but not yet sent. `base64`
// is the pure payload (no data-URI prefix); `loading` is true while FileReader
// is still decoding it. previewUrl is an object URL for the thumbnail.
export interface AttachedImage {
  id: string;
  file: File;
  base64: string;
  mediaType: string;
  previewUrl: string;
  loading: boolean;
}

// Transient state for a non-image file the user has attached but not yet sent.
// `base64` is the pure data payload (no data-URI prefix): for text/code files
// it's the base64 of the UTF-8 text, for PDF/DOCX it's the raw bytes. `loading`
// is true while FileReader decodes it. `tokenEstimate` drives the composer's
// context-window warning and the analytics prognosis (§3).
export interface AttachedFile {
  id: string;
  file: File;
  kind: import("@/lib/fileTypes").FileKind;
  filename: string;
  mediaType: string;
  sizeBytes: number;
  tokenEstimate: number;
  base64: string;
  loading: boolean;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  isStreaming?: boolean;
  // Extended thinking: the model's thinking text, whether it's actively
  // streaming (drives the animated "Thinking…" state), and the reasoning-token
  // count for the meta row.
  thinking?: string;
  isThinking?: boolean;
  reasoningTokens?: number;
  // Client-captured thinking timing: anchor for the live clock + frozen duration
  // shown in the collapsed header. Not persisted — reloaded history shows tokens
  // only (see spec §7).
  thinkingStartedAt?: number;
  thinkingDurationMs?: number;
  // Per-segment reasoning (multi-segment timeline, tr-cy0.3.8): accumulated live
  // from thinking_segment/thinking_delta/thinking_summary, restored on reload /
  // post-Stop. Undefined ⇒ legacy single segment built from `thinking`.
  thinkingSegments?: LiveThinkingSegment[];
  // Future backend-driven structured steps; empty today (dormant step kit).
  thinkingSteps?: ThinkingStep[];
  actualCost?: string;
  tokensUsed?: { input: number; output: number };
  contextTruncated?: boolean;
  errorMessage?: string;
  requestId?: string;
  // True while this optimistic user bubble has NO server row yet (set on send,
  // cleared by the message_done id swap). A send that dies before the backend
  // persists the message (e.g. a Redis-outage 503 at the idempotency gate)
  // leaves it set — resend must then fall back to a FRESH send: /edit against
  // the phantom genId would 404 forever (tr-w4fy.1).
  localOnly?: boolean;
  webSearchEnabled?: boolean;
  // True when this turn was stopped mid-reply (usage_logs.status == 'partial',
  // derived server-side). Drives the «Остановлено» terminal node / inline marker
  // instead of «Готово» (tr-fo1). Set on reload (mapResponseToMessage) and on the
  // post-Stop reconcile (applyMessageDone). Undefined ⇒ completed reply.
  stopped?: boolean;
  // Version history (migration 014). parentId = the turn this node replaces or
  // answers; siblings sharing parentId+role are versions of one turn.
  // versionIndex is the 1-based ordinal among siblings, versionCount the sibling
  // count (>1 reveals the <n/m> switcher). isActive marks the rendered path.
  // modelId is the per-version model (shown when versionCount > 1); createdAt
  // drives the user hover row's date-if-not-today. undefined on legacy bubbles.
  parentId?: string;
  isActive?: boolean;
  versionIndex?: number;
  versionCount?: number;
  modelId?: string;
  createdAt?: string;
  // Images shown with a sent user message. Only the preview URL + media type are
  // kept — never base64 — so multi-turn history doesn't bloat memory (§5).
  attachedImages?: { previewUrl: string; mediaType: string }[];
  // Files shown with a just-sent user message, before the post-send sync swaps
  // in the persisted copies (keyed by DB message_id in conversationFiles).
  // `blobUrl` is an object URL kept alive for the session so the file can be
  // opened/downloaded immediately, mirroring how images keep `previewUrl`.
  attachedFiles?: {
    filename: string;
    mediaType: string;
    sizeBytes: number;
    kind: import("@/lib/fileTypes").FileKind;
    tokenEstimate: number;
    blobUrl: string;
  }[];
}

export interface UseChatReturn {
  messages: ChatMessage[];
  isStreaming: boolean;
  error: string | null;
  webSearchEnabled: boolean;
  toggleWebSearch(): void;
  thinkingEnabled: boolean;
  thinkingEffort: ThinkingEffort;
  toggleThinking(): void;
  setThinkingEffort(effort: ThinkingEffort): void;
  toolsEnabled: boolean;
  toggleTools(): void;
  applyConversationSettings(s: {
    thinkingEnabled?: boolean | null;
    thinkingEffort?: ThinkingEffort | null;
    toolsEnabled?: boolean | null;
    webSearchEnabled?: boolean | null;
  }): void;
  sendMessage(
    content: string,
    modelId: string,
    conversationId: string,
    images?: AttachedImage[],
    files?: AttachedFile[],
  ): Promise<void>;
  stop(): void;
  setInitialMessages(messages: ChatMessage[]): void;
  clearError(): void;
  // Records which conversation the hook's actions (regenerate/edit/switch) act
  // on. The page sets it on load so reload-then-regenerate works without a send
  // having happened first.
  setActiveConversation(conversationId: string): void;
  // Branching actions (§6). regenerate streams a new assistant version into the
  // turn; editAndResend rewrites a user turn + restreams the reply; switchVersion
  // repoints the rendered active path to a chosen sibling.
  regenerate(
    assistantId: string,
    opts?: {
      modelId?: string;
      thinkingEnabled?: boolean;
      thinkingEffort?: ThinkingEffort;
      webSearchEnabled?: boolean;
    },
  ): Promise<void>;
  editAndResend(
    userId: string,
    content: string,
    opts?: {
      modelId?: string;
      thinkingEnabled?: boolean;
      thinkingEffort?: ThinkingEffort;
      webSearchEnabled?: boolean;
      toolsEnabled?: boolean;
    },
  ): Promise<void>;
  switchVersion(nodeId: string): Promise<void>;
  // Sibling-version cache keyed by parent_id, lazily filled on switcher open.
  conversationVersions: Map<string, MessageResponse[]>;
  loadConversationVersions(conversationId: string): Promise<void>;
  // Persisted images for the open conversation, keyed by their message_id, so
  // bubbles can show per-image context checkboxes and analytics can count them.
  conversationImages: Map<string, ContextImage[]>;
  loadConversationImages(conversationId: string): Promise<void>;
  clearConversationImages(): void;
  toggleImageContext(imageId: string): void;
  // Persisted files for the open conversation, keyed by their message_id, so
  // bubbles can show per-file context checkboxes and analytics can count them.
  conversationFiles: Map<string, ContextFile[]>;
  loadConversationFiles(conversationId: string): Promise<void>;
  clearConversationFiles(): void;
  toggleFileContext(fileId: string): void;
}

// Tokens arrive far faster than React can usefully repaint. Accumulate deltas
// in a ref and flush to state on a fixed cadence so long responses render
// smoothly instead of thrashing one setState per token (BUG 6).
const FLUSH_INTERVAL_MS = 50;

function genId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `msg-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

export function useChat(onMessageDone?: () => void): UseChatReturn {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  // Render-synced mirror so memoized callbacks (editAndResend) can inspect the
  // CURRENT transcript without taking `messages` as a dep (which would rebuild
  // them every token flush).
  const messagesMirrorRef = useRef<ChatMessage[]>(messages);
  messagesMirrorRef.current = messages;
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [webSearchEnabled, setWebSearchEnabled] = useState(false);
  const [thinkingEnabled, setThinkingEnabled] = useState(false);
  const [thinkingEffort, setThinkingEffortState] = useState<ThinkingEffort>("medium");
  const [toolsEnabled, setToolsEnabled] = useState(false);
  const [conversationImages, setConversationImages] = useState<
    Map<string, ContextImage[]>
  >(new Map());
  const [conversationFiles, setConversationFiles] = useState<
    Map<string, ContextFile[]>
  >(new Map());
  // Sibling-version cache for the < n/m > switcher, keyed by parent_id. Filled
  // lazily from GET /conversations/{id}?include=all on switcher open; cleared
  // when a new version lands so the next open re-fetches fresh counts.
  const [conversationVersions, setConversationVersions] = useState<
    Map<string, MessageResponse[]>
  >(new Map());
  const abortRef = useRef<AbortController | null>(null);
  // The conversation the branching actions target. Written by setActiveConversation
  // (page load) and by sendMessage, so regenerate/edit/switch read it without
  // being re-created on every render.
  const conversationIdRef = useRef<string | null>(null);

  // Mirror the images map into a ref so sendMessage reads the latest excluded
  // set without being re-created on every toggle.
  const conversationImagesRef = useRef(conversationImages);
  conversationImagesRef.current = conversationImages;
  const conversationFilesRef = useRef(conversationFiles);
  conversationFilesRef.current = conversationFiles;
  // Mirror the sibling cache into a ref so switchVersion reads the latest
  // siblings for its optimistic swap without being re-created on every change.
  const conversationVersionsRef = useRef(conversationVersions);
  conversationVersionsRef.current = conversationVersions;

  const toggleWebSearch = useCallback(() => {
    setWebSearchEnabled((prev) => !prev);
  }, []);

  const toggleThinking = useCallback(() => {
    setThinkingEnabled((prev) => !prev);
  }, []);

  const toggleTools = useCallback(() => {
    setToolsEnabled((prev) => !prev);
  }, []);

  const setThinkingEffort = useCallback((effort: ThinkingEffort) => {
    setThinkingEffortState(effort);
  }, []);

  // Restore the generation posture saved with a conversation's last request
  // (migration 020 / tr-hsg.12). null/undefined fields are skipped so an
  // un-recorded chat keeps the current/default toggles.
  const applyConversationSettings = useCallback(
    (s: {
      thinkingEnabled?: boolean | null;
      thinkingEffort?: ThinkingEffort | null;
      toolsEnabled?: boolean | null;
      webSearchEnabled?: boolean | null;
    }) => {
      if (s.thinkingEnabled != null) setThinkingEnabled(s.thinkingEnabled);
      if (s.thinkingEffort != null) setThinkingEffortState(s.thinkingEffort);
      if (s.toolsEnabled != null) setToolsEnabled(s.toolsEnabled);
      if (s.webSearchEnabled != null) setWebSearchEnabled(s.webSearchEnabled);
    },
    [],
  );

  const loadConversationImages = useCallback(async (conversationId: string) => {
    try {
      const dtos = await apiClient.get<ConversationImageDto[]>(
        `/api/v1/images/conversation/${conversationId}`,
      );
      const next = new Map<string, ContextImage[]>();
      for (const dto of dtos) {
        if (!dto.message_id) continue;
        const { included_in_context, ...rest } = dto;
        const img: ContextImage = {
          ...rest,
          includedInContext: included_in_context,
        };
        const bucket = next.get(dto.message_id);
        if (bucket) bucket.push(img);
        else next.set(dto.message_id, [img]);
      }
      setConversationImages(next);
    } catch {
      // Non-fatal: checkboxes/analytics just degrade to no-image state.
      setConversationImages(new Map());
    }
  }, []);

  const clearConversationImages = useCallback(() => {
    setConversationImages(new Map());
  }, []);

  const loadConversationFiles = useCallback(async (conversationId: string) => {
    try {
      const dtos = await apiClient.get<ConversationFileDto[]>(
        `/api/v1/files/conversation/${conversationId}`,
      );
      const next = new Map<string, ContextFile[]>();
      for (const dto of dtos) {
        if (!dto.message_id) continue;
        const { included_in_context, ...rest } = dto;
        const file: ContextFile = {
          ...rest,
          includedInContext: included_in_context,
        };
        const bucket = next.get(dto.message_id);
        if (bucket) bucket.push(file);
        else next.set(dto.message_id, [file]);
      }
      setConversationFiles(next);
    } catch {
      // Non-fatal: checkboxes/analytics just degrade to no-file state.
      setConversationFiles(new Map());
    }
  }, []);

  const clearConversationFiles = useCallback(() => {
    setConversationFiles(new Map());
  }, []);

  const toggleImageContext = useCallback((imageId: string) => {
    const prev = conversationImagesRef.current;
    let current: boolean | null = null;
    for (const imgs of prev.values()) {
      const hit = imgs.find((img) => img.id === imageId);
      if (hit) {
        current = hit.includedInContext;
        break;
      }
    }
    if (current === null) return;
    const nextValue = !current;

    const next = new Map<string, ContextImage[]>();
    for (const [msgId, imgs] of prev) {
      next.set(
        msgId,
        imgs.map((img) =>
          img.id === imageId ? { ...img, includedInContext: nextValue } : img,
        ),
      );
    }
    setConversationImages(next);

    // Persist the toggle so it survives reload and the backend skips excluded
    // images in context (tr-kx4f). Roll back the optimistic flip if it fails, so
    // the checkbox never lies about what the model actually receives.
    void apiClient
      .patch(`/api/v1/images/${imageId}`, { included_in_context: nextValue })
      .catch(() => setConversationImages(prev));
  }, []);

  const toggleFileContext = useCallback((fileId: string) => {
    const prev = conversationFilesRef.current;
    let current: boolean | null = null;
    for (const files of prev.values()) {
      const hit = files.find((f) => f.id === fileId);
      if (hit) {
        current = hit.includedInContext;
        break;
      }
    }
    if (current === null) return;
    const nextValue = !current;

    const next = new Map<string, ContextFile[]>();
    for (const [msgId, files] of prev) {
      next.set(
        msgId,
        files.map((f) =>
          f.id === fileId ? { ...f, includedInContext: nextValue } : f,
        ),
      );
    }
    setConversationFiles(next);

    void apiClient
      .patch(`/api/v1/files/${fileId}`, { included_in_context: nextValue })
      .catch(() => setConversationFiles(prev));
  }, []);

  const setInitialMessages = useCallback((initial: ChatMessage[]) => {
    // Swap the displayed transcript WITHOUT aborting any in-flight stream.
    // Switching chats mid-stream must let the background request finish so the
    // backend persists the assistant message (BUG 2/6) — the stream writes
    // target the original assistant id, which is a harmless no-op once the
    // displayed array has been replaced. Explicit cancels go through stop().
    setMessages(initial);
  }, []);

  const clearError = useCallback(() => setError(null), []);

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  // After a Stop, the server finalized the partial on a fresh session but could
  // not emit message_done (the SSE connection is closed). Poll the persisted
  // result and patch the bubble in place via the same reducer the happy path
  // uses — so cost/token badges appear seamlessly, no reload (tr-sgz).
  const reconcileAfterStop = useCallback(
    async (
      conversationId: string,
      requestId: string,
      assistantId: string,
      userMessageId: string,
    ) => {
      for (let attempt = 0; attempt < 5; attempt++) {
        await new Promise((r) => setTimeout(r, 500));
        try {
          const data = await apiClient.get<SSEMessageDone>(
            `/api/v1/chat/conversations/${conversationId}/requests/${requestId}`,
          );
          setMessages((prev) => applyMessageDone(prev, assistantId, data, userMessageId));
          return;
        } catch (err) {
          // 404 is TERMINAL: an orphaned / very-early-stopped generation never
          // persisted a result, so it will 404 forever — retrying 5× is needless
          // console noise + wasted requests (tr-kl5). Give up silently after one
          // attempt. Transient/5xx (and network status 0) MAY persist shortly, so
          // those keep the short retry; a clean Stop WITH a partial still
          // reconciles on the first 200 (tr-tc0/tr-sgz unaffected).
          if (err instanceof ApiRequestError && err.status === 404) {
            // Nothing was persisted for this request (a very-early Stop aborted
            // before the result row existed). The USER message was still saved
            // before streaming, so the optimistic genIds now point at rows the
            // server doesn't know — a follow-up edit/regenerate/resend would 404
            // ("Ошибка отправки сообщения", needing a reload). Refetch the
            // conversation's active transcript and reconcile to the real ids so
            // those actions work without a reload (tr-hsg.13).
            try {
              const conv = await apiClient.get<ConversationResponse>(
                `/api/v1/chat/conversations/${conversationId}`,
              );
              const mapped = (conv.messages ?? [])
                .filter((m) => m.role === "user" || m.role === "assistant")
                .map(mapResponseToMessage);
              setMessages(mapped);
            } catch {
              // Leave the optimistic bubbles; manual reload remains the fallback.
            }
            return;
          }
          // Other errors → retry; reload still works as a last resort.
        }
      }
    },
    [setMessages],
  );

  const sendMessage = useCallback(
    async (
      content: string,
      modelId: string,
      conversationId: string,
      images?: AttachedImage[],
      files?: AttachedFile[],
    ) => {
      const token = getAccessToken();
      if (!token) {
        setError("Сессия истекла. Войдите снова.");
        return;
      }
      // Remember the conversation so the branching actions can target it later.
      conversationIdRef.current = conversationId;

      // Idempotency key (tr-n5b): one fresh UUID per send ATTEMPT. The frontend
      // submit lock already stops the same-tick double-submit; this id is the
      // backend defense-in-depth so a multi-tab / HTTP-retry / programmatic
      // replay of the SAME logical send is deduped before reserving/charging. A
      // retried identical POST reuses this id; a fresh submit gets a new one.
      const clientRequestId = genId();

      // Build the wire content fresh at send time: text first, then images as
      // data-URI image_url parts, then files as `file` parts (OpenRouter's
      // recommended order). Text-only messages keep the plain-string form so
      // that path is byte-identical. The caller passes only attachments the
      // current model supports (§4) — unsupported ones are excluded here by
      // simply never being in these arrays.
      const hasImages = (images?.length ?? 0) > 0;
      const hasFiles = (files?.length ?? 0) > 0;
      const requestContent: string | MessageContentPart[] =
        hasImages || hasFiles
          ? [
              ...(content ? [{ type: "text" as const, text: content }] : []),
              ...(images ?? []).map((img) => ({
                type: "image_url" as const,
                image_url: {
                  url: `data:${img.mediaType};base64,${img.base64}`,
                },
              })),
              ...(files ?? []).map((f) => ({
                type: "file" as const,
                file: {
                  filename: f.filename,
                  file_data: `data:${f.mediaType};base64,${f.base64}`,
                },
              })),
            ]
          : content;

      const userMessage: ChatMessage = {
        id: genId(),
        role: "user",
        content,
        localOnly: true,
        // Keep only preview URL + media type — NEVER base64 (§5).
        ...(hasImages
          ? {
              attachedImages: images!.map((img) => ({
                previewUrl: img.previewUrl,
                mediaType: img.mediaType,
              })),
            }
          : {}),
        // Display metadata + a session object URL so the file can be opened
        // immediately, before the post-send sync swaps in the persisted copy.
        ...(hasFiles
          ? {
              attachedFiles: files!.map((f) => ({
                filename: f.filename,
                mediaType: f.mediaType,
                sizeBytes: f.sizeBytes,
                kind: f.kind,
                tokenEstimate: f.tokenEstimate,
                blobUrl: URL.createObjectURL(f.file),
              })),
            }
          : {}),
      };
      const assistantId = genId();
      const assistantMessage: ChatMessage = {
        id: assistantId,
        role: "assistant",
        content: "",
        isStreaming: true,
        webSearchEnabled,
      };

      setMessages((prev) => [...prev, userMessage, assistantMessage]);
      setIsStreaming(true);
      setError(null);

      const controller = new AbortController();
      abortRef.current = controller;

      // Off-React accumulation shared with runActionStream via streamAccumulator
      // (tr-sja). The buffer mutations are identical between the two handlers;
      // only the side-effects on the first reasoning event / first answer token
      // differ, injected here through `hooks`.
      const buffers = createStreamBuffers();
      // sendMessage-only locals driving the thinking collapse + frozen duration:
      // thinkingStartAt anchors the live clock, contentStarted gates the one-shot
      // collapse on the first answer token.
      let thinkingStarted = false;
      let thinkingStartAt: number | null = null;
      let contentStarted = false;
      const patchAssistant = (patch: Partial<ChatMessage>) => {
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantId ? { ...m, ...patch } : m)),
        );
      };
      const hooks: StreamEventHooks = {
        // First reasoning event of any kind: mark thinking active and remember
        // the start for the collapse + duration below.
        onThinkingStart: () => {
          if (thinkingStarted) return;
          thinkingStarted = true;
          thinkingStartAt = Date.now();
          patchAssistant({ isThinking: true, thinkingStartedAt: thinkingStartAt });
        },
        // First answer token means thinking is done — collapse the animated
        // state so the spinner stops, and freeze its duration.
        onFirstContent: () => {
          if (contentStarted) return;
          contentStarted = true;
          if (thinkingStarted) {
            patchAssistant({
              isThinking: false,
              thinkingDurationMs:
                thinkingStartAt != null ? Date.now() - thinkingStartAt : undefined,
            });
          }
        },
      };
      const flush = () => {
        const snap = snapshotBuffers(buffers);
        if (!snap) return;
        setMessages((prev) =>
          prev.map((m) => {
            if (m.id !== assistantId) return m;
            const next = { ...m };
            if (snap.content) next.content = m.content + snap.content;
            if (snap.thinking) next.thinking = (m.thinking ?? "") + snap.thinking;
            if (snap.segments) next.thinkingSegments = snap.segments;
            if (snap.steps) next.thinkingSteps = snap.steps;
            return next;
          }),
        );
      };
      const flushTimer = setInterval(flush, FLUSH_INTERVAL_MS);

      // History images the user toggled off are excluded from re-injection.
      const excludedImageIds: string[] = [];
      for (const imgs of conversationImagesRef.current.values()) {
        for (const img of imgs) {
          if (!img.includedInContext) excludedImageIds.push(img.id);
        }
      }
      // Same for files toggled off.
      const excludedFileIds: string[] = [];
      for (const fs of conversationFilesRef.current.values()) {
        for (const f of fs) {
          if (!f.includedInContext) excludedFileIds.push(f.id);
        }
      }

      let errored = false;
      let aborted = false;
      // The connection dropped mid-stream (network break, not a Stop and not a
      // clean SSE error event). Treated like an abort for reconciliation so the
      // optimistic ids get swapped for the real transcript and a follow-up
      // resend/edit doesn't 404 + lose the message (tr-rydu).
      let connectionBroke = false;
      let requestIdForPoll: string | null = null;
      try {
        for await (const evt of streamChatMessage(
          conversationId,
          requestContent,
          modelId,
          token,
          {
            signal: controller.signal,
            clientRequestId,
            webSearchEnabled,
            thinkingEnabled,
            thinkingEffort,
            toolsEnabled,
            excludedImageIds,
            excludedFileIds,
          },
        )) {
          // Common content/thinking/tool accumulation (shared with runActionStream).
          if (applyStreamEvent(buffers, evt, hooks)) continue;

          if (evt.event === "message_start") {
            const data = evt.data as SSEMessageStart;
            requestIdForPoll = data.request_id;
            patchAssistant({ requestId: data.request_id });
          } else if (evt.event === "context_truncated") {
            patchAssistant({ contextTruncated: true });
          } else if (evt.event === "message_done") {
            const data = evt.data as SSEMessageDone;
            // Drain buffered text + thinking first, then finalize via the shared
            // reducer so the genId placeholder is swapped for data.message_id and
            // cost/tokens land in the SAME update — no flicker (BUG 2).
            const drained = drainBuffers(buffers);
            setMessages((prev) =>
              applyMessageDone(
                prev.map((m) =>
                  m.id === assistantId
                    ? {
                        ...m,
                        content: m.content + drained.content,
                        thinking: drained.thinking
                          ? (m.thinking ?? "") + drained.thinking
                          : m.thinking,
                        // Live message_done carries no segments on the DTO, so
                        // applyMessageDone keeps whatever we set here.
                        thinkingSegments: drained.segments ?? m.thinkingSegments,
                        thinkingSteps: drained.steps ?? m.thinkingSteps,
                        isThinking: false,
                        thinkingDurationMs:
                          m.thinkingDurationMs ??
                          (m.thinkingStartedAt != null
                            ? Date.now() - m.thinkingStartedAt
                            : undefined),
                      }
                    : m,
                ),
                assistantId,
                data,
                userMessage.id,
              ),
            );
            onMessageDone?.();
          } else if (evt.event === "error") {
            const data = evt.data as Partial<SSEError>;
            const message =
              data.code === "insufficient_funds"
                ? formatInsufficientFunds(data.required, data.available)
                : getErrorMessage(data.code, data.message);
            errored = true;
            flush();
            patchAssistant({ isStreaming: false, errorMessage: message });
            setError(message);
            onMessageDone?.();
            break;
          }
        }
      } catch {
        if (controller.signal.aborted) {
          aborted = true;
        } else {
          connectionBroke = true;
          const fallback = "Ошибка соединения. Попробуйте ещё раз.";
          flush();
          setError(fallback);
          patchAssistant({ isStreaming: false, errorMessage: fallback });
        }
      } finally {
        clearInterval(flushTimer);
        // A Stop/abort (or an abnormal end without message_done) can freeze a tool
        // step mid-flight in `running` — its emerald pulse then ticks forever next
        // to a «Готово» rail (tr-3fh). Coerce any still-running step to `error`
        // before the final flush so the snapshot reconciles the live bubble.
        // Mirrors the tool_loop_halted handler; tool_steps aren't persisted on a
        // hard Stop (§12), so this reconciles the LIVE state only.
        if (controller.signal.aborted) {
          coerceRunningStepsToError(buffers);
        }
        flush();
        // streamChatMessage swallows the AbortError internally (sse.ts), so a
        // Stop ends the `for await` normally and the catch above never runs —
        // derive `aborted` from the controller here instead of relying on a
        // thrown error (tr-9i5). Without this the post-Stop balance refresh below
        // never fired, leaving the displayed balance stale after a partially
        // billed Stop (tr-04a finalizes the partial server-side).
        if (controller.signal.aborted) aborted = true;
        // Normal stream-end (no message_done) or an abort leaves the bubble
        // marked streaming — clear it so the caret stops blinking and the
        // partial text stays as-is.
        if (!errored) {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId && (m.isStreaming || m.isThinking)
                ? { ...m, isStreaming: false, isThinking: false }
                : m,
            ),
          );
        }
        setIsStreaming(false);
        if (abortRef.current === controller) abortRef.current = null;
        // A stopped stream may have been partially billed server-side
        // (ClientDisconnectedError → finalize_partial), so refresh balance AND
        // reconcile the bubble's id/cost/tokens seamlessly (tr-sgz).
        if (aborted || connectionBroke) {
          onMessageDone?.();
          if (conversationId && requestIdForPoll) {
            void reconcileAfterStop(
              conversationId,
              requestIdForPoll,
              assistantId,
              userMessage.id,
            );
          }
        }
        // The genId→message_id swap (applyMessageDone) already gives bubbles
        // their DB ids, so we no longer re-fetch the whole conversation on the
        // happy path. Only attachment maps need refreshing so the just-sent
        // thumbnails/chips pick up their persisted ids and context checkboxes.
        if ((hasImages || hasFiles) && !errored && !aborted) {
          void loadConversationImages(conversationId);
          void loadConversationFiles(conversationId);
        }
      }
    },
    [
      onMessageDone,
      webSearchEnabled,
      thinkingEnabled,
      thinkingEffort,
      toolsEnabled,
      loadConversationImages,
      loadConversationFiles,
      reconcileAfterStop,
    ],
  );

  const setActiveConversation = useCallback((conversationId: string) => {
    conversationIdRef.current = conversationId;
  }, []);

  const loadConversationVersions = useCallback(
    async (conversationId: string) => {
      try {
        const conv = await apiClient.get<ConversationResponse>(
          `/api/v1/chat/conversations/${conversationId}?include=all`,
        );
        const byParent = new Map<string, MessageResponse[]>();
        for (const m of conv.messages ?? []) {
          // Root-turn versions have parent_id === null; bucket them under "" so
          // the FIRST turn's edited versions stay navigable by the switcher
          // (siblingNeighbors falls back to the same "" key).
          const key = m.parent_id ?? "";
          const bucket = byParent.get(key);
          if (bucket) bucket.push(m);
          else byParent.set(key, [m]);
        }
        setConversationVersions(byParent);
      } catch {
        setConversationVersions(new Map());
      }
    },
    [],
  );

  // Run an SSE action stream (regenerate/edit) against a placeholder assistant
  // bubble. Mirrors sendMessage's buffered flush + applyMessageDone finalize,
  // but the URL/body come from the supplied generator. The placeholder id is
  // closured so switching versions mid-stream can't repoint THIS stream's
  // patches (streaming-vs-switch guard).
  const runActionStream = useCallback(
    async (
      events: AsyncGenerator<SSEEventPayload>,
      placeholderId: string,
      conversationId: string,
      controller: AbortController,
    ) => {
      setIsStreaming(true);
      setError(null);
      // Off-React accumulation shared with sendMessage via streamAccumulator
      // (tr-sja): content + flat thinking + per-segment reasoning + agentic tool
      // steps are built up off-React and snapshotted on each flush, so a
      // regenerated/edited version renders the SAME thinking timeline + tool
      // cards as a fresh send (not a flat single-shot). runActionStream passes
      // only onThinkingStart — NO onFirstContent, so (unlike sendMessage) it does
      // not collapse the thinking state on the first answer token, preserving the
      // pre-refactor behavior exactly.
      const buffers = createStreamBuffers();
      let thinkingStarted = false;
      let thinkingStartAt = 0;
      const hooks: StreamEventHooks = {
        onThinkingStart: () => {
          if (thinkingStarted) return;
          thinkingStarted = true;
          thinkingStartAt = Date.now();
          setMessages((prev) =>
            prev.map((m) =>
              m.id === placeholderId
                ? { ...m, isThinking: true, thinkingStartedAt: thinkingStartAt }
                : m,
            ),
          );
        },
      };
      const flush = () => {
        const snap = snapshotBuffers(buffers);
        if (!snap) return;
        setMessages((prev) =>
          prev.map((m) => {
            if (m.id !== placeholderId) return m;
            const next = { ...m };
            if (snap.content) next.content = m.content + snap.content;
            if (snap.thinking) next.thinking = (m.thinking ?? "") + snap.thinking;
            if (snap.segments) next.thinkingSegments = snap.segments;
            if (snap.steps) next.thinkingSteps = snap.steps;
            return next;
          }),
        );
      };
      const timer = setInterval(flush, FLUSH_INTERVAL_MS);
      let errored = false;
      try {
        for await (const evt of events) {
          // Common content/thinking/tool accumulation (shared with sendMessage).
          if (applyStreamEvent(buffers, evt, hooks)) continue;

          if (evt.event === "message_start") {
            const d = evt.data as SSEMessageStart;
            setMessages((prev) =>
              prev.map((m) =>
                m.id === placeholderId ? { ...m, requestId: d.request_id } : m,
              ),
            );
          } else if (evt.event === "message_done") {
            const d = evt.data as SSEMessageDone;
            const drained = drainBuffers(buffers);
            setMessages((prev) =>
              applyMessageDone(
                prev.map((m) =>
                  m.id === placeholderId
                    ? {
                        ...m,
                        content: m.content + drained.content,
                        thinking: drained.thinking
                          ? (m.thinking ?? "") + drained.thinking
                          : m.thinking,
                        thinkingSegments: drained.segments ?? m.thinkingSegments,
                        thinkingSteps: drained.steps ?? m.thinkingSteps,
                        isStreaming: false,
                        isThinking: false,
                        thinkingDurationMs:
                          m.thinkingDurationMs ??
                          (m.thinkingStartedAt != null
                            ? Date.now() - m.thinkingStartedAt
                            : undefined),
                      }
                    : m,
                ),
                placeholderId,
                d,
              ),
            );
            // A new version landed — drop the stale sibling cache for this turn
            // so the switcher re-fetches fresh counts on next open.
            setConversationVersions(new Map());
            onMessageDone?.();
          } else if (evt.event === "error") {
            const d = evt.data as Partial<SSEError>;
            const message =
              d.code === "insufficient_funds"
                ? formatInsufficientFunds(d.required, d.available)
                : getErrorMessage(d.code, d.message);
            errored = true;
            flush();
            setMessages((prev) =>
              prev.map((m) =>
                m.id === placeholderId
                  ? { ...m, isStreaming: false, isThinking: false, errorMessage: message }
                  : m,
              ),
            );
            setError(message);
            onMessageDone?.();
            break;
          }
        }
      } catch {
        if (!controller.signal.aborted) {
          const fallback = "Ошибка соединения. Попробуйте ещё раз.";
          flush();
          setError(fallback);
          setMessages((prev) =>
            prev.map((m) =>
              m.id === placeholderId
                ? { ...m, isStreaming: false, isThinking: false, errorMessage: fallback }
                : m,
            ),
          );
        }
      } finally {
        clearInterval(timer);
        // Stop/abort parity (tr-3fh): coerce any still-running step to error
        // before the final flush so it doesn't pulse forever on the new version.
        if (controller.signal.aborted) {
          coerceRunningStepsToError(buffers);
        }
        flush();
        if (!errored) {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === placeholderId && (m.isStreaming || m.isThinking)
                ? { ...m, isStreaming: false, isThinking: false }
                : m,
            ),
          );
        }
        setIsStreaming(false);
        if (abortRef.current === controller) abortRef.current = null;
        void loadConversationImages(conversationId);
        void loadConversationFiles(conversationId);
        // Refresh the active path so the just-created version's < n/m > switcher
        // appears immediately (the streamed bubble carries no version_count until
        // a GET re-derives it). This also runs on error/abort: after a failed or
        // stopped regenerate/edit the server restores the prior answer (or keeps
        // the finalized partial) as active (tr-hvr1), and without re-syncing the
        // client would show the empty/error placeholder until a manual reload —
        // a stale client/server divergence (tr-dfjx).
        // On the happy path the server already holds the coherent transcript, so
        // the first GET returns the assistant-led tail and we stop immediately.
        // On a Stop/error the server restores the prior answer (or finalized
        // partial) as active on a FRESH, shielded session that can commit a beat
        // AFTER the abort — so a single immediate GET can read the pre-restore
        // state (active leaf still on the user message) and render an empty slot +
        // a transient error until reload. Poll a few times until the active tail
        // is an assistant (coherent) before rendering, so the restored answer
        // appears live without a reload (tr-dfjx race → tr-i1ve).
        for (let attempt = 0; attempt < 5; attempt++) {
          try {
            const conv = await apiClient.get<ConversationResponse>(
              `/api/v1/chat/conversations/${conversationId}`,
            );
            const mapped = (conv.messages ?? [])
              .filter((m) => m.role === "user" || m.role === "assistant")
              .map(mapResponseToMessage);
            const coherent =
              mapped.length > 0 &&
              mapped[mapped.length - 1].role === "assistant";
            if (coherent || attempt === 4) {
              if (mapped.length > 0) setMessages(mapped);
              break;
            }
          } catch {
            // Non-fatal: the switcher count / restored answer appears on reload.
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, 400));
        }
      }
    },
    [onMessageDone, loadConversationImages, loadConversationFiles],
  );

  // Regenerate an assistant turn: truncate the rendered transcript at the
  // target (its stale tail is now an inactive branch), append a fresh streaming
  // placeholder, and stream the new sibling. opts override model/thinking/web.
  const regenerate = useCallback(
    async (
      assistantId: string,
      opts?: {
        modelId?: string;
        thinkingEnabled?: boolean;
        thinkingEffort?: ThinkingEffort;
        webSearchEnabled?: boolean;
        toolsEnabled?: boolean;
      },
    ): Promise<void> => {
      const token = getAccessToken();
      if (!token) {
        setError("Сессия истекла. Войдите снова.");
        return;
      }
      const conversationId = conversationIdRef.current;
      if (!conversationId) return;
      const placeholderId = genId();
      setMessages((prev) => {
        const idx = prev.findIndex((m) => m.id === assistantId);
        const head = idx === -1 ? prev : prev.slice(0, idx);
        return [
          ...head,
          {
            id: placeholderId,
            role: "assistant",
            content: "",
            isStreaming: true,
          },
        ];
      });
      const controller = new AbortController();
      abortRef.current = controller;
      // Default every knob to the LIVE composer state so a bare onRegenerate(id)
      // from MessageBubble keeps the chat's tool/thinking/web mode; opts still
      // override per-call (e.g. a model switch). Without this the regenerated
      // version lost tools/thinking/web entirely (tr-1hm).
      await runActionStream(
        streamRegenerate(conversationId, assistantId, token, {
          signal: controller.signal,
          modelId: opts?.modelId,
          thinkingEnabled: opts?.thinkingEnabled ?? thinkingEnabled,
          thinkingEffort: opts?.thinkingEffort ?? thinkingEffort,
          webSearchEnabled: opts?.webSearchEnabled ?? webSearchEnabled,
          toolsEnabled: opts?.toolsEnabled ?? toolsEnabled,
        }),
        placeholderId,
        conversationId,
        controller,
      );
    },
    [runActionStream, thinkingEnabled, thinkingEffort, webSearchEnabled, toolsEnabled],
  );

  // Edit a user turn (text only) and resend: truncate at the target, append an
  // edited user bubble + a streaming assistant placeholder, stream the reply.
  // opts override model/thinking/web/tools per-call (e.g. a picker model switch).
  const editAndResend = useCallback(
    async (
      userId: string,
      content: string,
      opts?: {
        modelId?: string;
        thinkingEnabled?: boolean;
        thinkingEffort?: ThinkingEffort;
        webSearchEnabled?: boolean;
        toolsEnabled?: boolean;
      },
    ): Promise<void> => {
      const token = getAccessToken();
      if (!token) {
        setError("Сессия истекла. Войдите снова.");
        return;
      }
      const conversationId = conversationIdRef.current;
      if (!conversationId) return;
      // A never-persisted optimistic bubble (send failed before the server
      // saved it) has no DB row — /edit would 404 forever. Drop the stale
      // bubbles and re-send the same content as a fresh message (tr-w4fy.1).
      const target = messagesMirrorRef.current.find((m) => m.id === userId);
      if (target?.localOnly && opts?.modelId) {
        setMessages((prev) => {
          const idx = prev.findIndex((m) => m.id === userId);
          return idx === -1 ? prev : prev.slice(0, idx);
        });
        await sendMessage(content, opts.modelId, conversationId);
        return;
      }
      const placeholderId = genId();
      setMessages((prev) => {
        const idx = prev.findIndex((m) => m.id === userId);
        const head = idx === -1 ? prev : prev.slice(0, idx);
        return [
          ...head,
          { id: genId(), role: "user", content },
          {
            id: placeholderId,
            role: "assistant",
            content: "",
            isStreaming: true,
          },
        ];
      });
      const controller = new AbortController();
      abortRef.current = controller;
      // Carry the live model picker (tr-77u) + composer toggles (tr-1hm) into the
      // forced reply so editing a question uses the CURRENTLY-selected model and
      // keeps the chat's tool/thinking/web mode. Each defaults to the live state
      // so a bare editAndResend(id, text) still matches the composer; opts win.
      await runActionStream(
        streamEdit(conversationId, userId, content, token, {
          signal: controller.signal,
          modelId: opts?.modelId,
          thinkingEnabled: opts?.thinkingEnabled ?? thinkingEnabled,
          thinkingEffort: opts?.thinkingEffort ?? thinkingEffort,
          webSearchEnabled: opts?.webSearchEnabled ?? webSearchEnabled,
          toolsEnabled: opts?.toolsEnabled ?? toolsEnabled,
        }),
        placeholderId,
        conversationId,
        controller,
      );
    },
    [
      runActionStream,
      sendMessage,
      thinkingEnabled,
      thinkingEffort,
      webSearchEnabled,
      toolsEnabled,
    ],
  );

  // Switch the rendered active path to a chosen sibling version. Optimistic:
  // re-derive messages[] from the cached siblings first (instant feedback), then
  // persist via PUT and reconcile from the server-authoritative path. Must NOT
  // abort any in-flight stream (no abortRef.abort()), per the race guard: a
  // background stream keeps writing to an id no longer rendered — a harmless
  // no-op, mirroring setInitialMessages.
  const switchVersion = useCallback(
    async (nodeId: string): Promise<void> => {
      const conversationId = conversationIdRef.current;
      if (!conversationId) return;
      // Optimistic local swap from the sibling cache (if loaded) so the bubble
      // updates before the PUT round-trips.
      for (const siblings of conversationVersionsRef.current.values()) {
        if (siblings.some((s) => s.id === nodeId)) {
          setMessages((prev) => deriveActivePath(prev, siblings, nodeId));
          break;
        }
      }
      try {
        // The endpoint returns the new active path as a bare MessageResponse[]
        // (NOT a ConversationResponse) — map it straight into the transcript.
        const path = await apiClient.put<MessageResponse[]>(
          `/api/v1/chat/conversations/${conversationId}/active-path`,
          { node_id: nodeId },
        );
        const mapped = (path ?? [])
          .filter((m) => m.role === "user" || m.role === "assistant")
          .map(mapResponseToMessage);
        setMessages(mapped);
        const ids = new Set(activePathIds(mapped));
        // Scope attachment maps to the new active path so an inactive sibling's
        // images/files never render on the wrong bubble.
        setConversationImages((prev) => {
          const next = new Map<string, ContextImage[]>();
          for (const [k, v] of prev) if (ids.has(k)) next.set(k, v);
          return next;
        });
        setConversationFiles((prev) => {
          const next = new Map<string, ContextFile[]>();
          for (const [k, v] of prev) if (ids.has(k)) next.set(k, v);
          return next;
        });
      } catch {
        setError("Не удалось переключить версию. Попробуйте ещё раз.");
      }
    },
    [],
  );

  return {
    messages,
    isStreaming,
    error,
    webSearchEnabled,
    toggleWebSearch,
    thinkingEnabled,
    thinkingEffort,
    toggleThinking,
    setThinkingEffort,
    toolsEnabled,
    toggleTools,
    applyConversationSettings,
    sendMessage,
    stop,
    setInitialMessages,
    clearError,
    setActiveConversation,
    regenerate,
    editAndResend,
    switchVersion,
    conversationVersions,
    loadConversationVersions,
    conversationImages,
    loadConversationImages,
    clearConversationImages,
    toggleImageContext,
    conversationFiles,
    loadConversationFiles,
    clearConversationFiles,
    toggleFileContext,
  };
}
