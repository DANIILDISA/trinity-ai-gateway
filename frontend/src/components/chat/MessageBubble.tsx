"use client";

import { memo, useEffect, useState, type ReactNode } from "react";

import { NeuralIcon } from "@/components/ui/NeuralIcon";
import { Tooltip } from "@/components/ui/Tooltip";
import { ClampedContent } from "./ClampedContent";
import { Markdown } from "./Markdown";
import { RevealMarkdown } from "./streaming/RevealMarkdown";
import { ArtifactCard } from "./ArtifactCard";
import { ToolArtifactCard } from "./ToolArtifactCard";
import { VersionSwitcher } from "./VersionSwitcher";
import { extractArtifacts, type Artifact } from "@/lib/artifacts";
import { deriveToolArtifacts } from "@/lib/toolArtifact";
import { downloadArtifact } from "@/lib/downloadArtifact";
import {
  formatCost,
  formatModelName,
  formatRelativeDate,
  formatTokenCount,
} from "@/lib/formatting";
import { estimatePrognosis } from "@/lib/prognosis";
import { formatFileSize } from "@/lib/fileTypes";
import { Lightbox } from "./Lightbox";
import {
  FileViewerModal,
  downloadFile,
  isViewableKind,
  type FileTarget,
} from "./FileViewer";
import type { ChatMessage } from "@/hooks/useChat";
import type {
  ContextFile,
  ContextImage,
  FileKind,
  MessageResponse,
  ModelInfo,
} from "@/types/api";
import { ThinkingProcess, buildThinkingProcess } from "@/components/chat/thinking";

export interface MessageBubbleProps {
  message: ChatMessage;
  isLast: boolean;
  // Persisted images for this message (from the conversation context map).
  // When present, thumbnails get a per-image context-inclusion checkbox.
  images?: ContextImage[];
  // Persisted files for this message. When present, chips get a per-file
  // context-inclusion checkbox (parity with images).
  files?: ContextFile[];
  // Cumulative input+output tokens of the preceding active-path messages
  // (produced by ChatWindow, §0.8). Feeds the inline edit box's live prognosis
  // so it forecasts edited text + this branch's preceding context.
  precedingHistoryTokens?: number;
  onToggleImageContext?: (imageId: string) => void;
  onToggleFileContext?: (fileId: string) => void;
  // Version history (tr-x81 / §6). All optional so legacy call sites and tests
  // that omit them keep compiling.
  //
  // Sibling cache keyed by parent_id (from useChat.conversationVersions). Used to
  // resolve the prev/next node id for the VersionSwitcher; lazily filled by
  // onLoadVersions when a switcher renders but its bucket is missing.
  conversationVersions?: Map<string, MessageResponse[]>;
  onLoadVersions?: () => void;
  // Switch the active path to a chosen sibling node (useChat.switchVersion).
  onSwitchVersion?: (nodeId: string) => void;
  // Regenerate an assistant turn — opts.modelId overrides the model for a
  // per-version model picker (deferred); plain regenerate reuses settings.
  onRegenerate?: (assistantId: string, opts?: { modelId?: string }) => void;
  // Edit a user turn's text and restream the reply (useChat.editAndResend).
  onEditAndResend?: (userId: string, content: string) => void;
  // Re-run a user turn unchanged (resend); wired to editAndResend(content).
  onResend?: (userId: string) => void;
  // Selected model — supplies prices for the inline edit-box live prognosis.
  selectedModel?: ModelInfo;
  // Opens a generated document/code artifact in the canvas panel (light path).
  onOpenArtifact?: (artifact: Artifact) => void;
  // Active conversation id — keys the tool-artifact download endpoint (tr-cy0.2.13).
  conversationId?: string;
}

// Resolve the previous/next sibling node id for a branched turn from the
// parent-keyed sibling cache. Siblings are ordered (created_at ASC, id ASC) to
// match the backend's derive_version_info ordering, so position aligns with the
// 1-based version_index. Returns undefined when the cache is cold or the message
// isn't found among its siblings.
function siblingNeighbors(
  message: ChatMessage,
  versions: Map<string, MessageResponse[]> | undefined,
): { prevId?: string; nextId?: string } {
  if (!versions) return {};
  // Root-turn versions (parent_id === null) are bucketed under "" — see
  // loadConversationVersions. Falling back to "" makes the first turn navigable.
  const bucket = versions.get(message.parentId ?? "");
  if (!bucket || bucket.length <= 1) return {};
  const siblings = [...bucket]
    .filter((s) => s.role === message.role)
    .sort((a, b) => {
      const t = a.created_at.localeCompare(b.created_at);
      return t !== 0 ? t : a.id.localeCompare(b.id);
    });
  const pos = siblings.findIndex((s) => s.id === message.id);
  if (pos === -1) return {};
  return {
    prevId: pos > 0 ? siblings[pos - 1].id : undefined,
    nextId: pos < siblings.length - 1 ? siblings[pos + 1].id : undefined,
  };
}

// A persisted thumbnail with a checkbox to include/exclude it from the context
// re-injected on the next turn. Checked = in context (green); unchecked = gray.
function ContextCheckbox({
  checked,
  onToggle,
  disabled = false,
  disabledTitle,
}: {
  checked: boolean;
  onToggle: () => void;
  // tr-hwa8: when the active model can't consume this attachment type (image on a
  // text-only model), it can NEVER be in context — force the OFF visual and block
  // toggling so the green "в контексте" can't lie about what the model receives.
  disabled?: boolean;
  disabledTitle?: string;
}) {
  const effectiveChecked = checked && !disabled;
  const label = disabled
    ? disabledTitle ?? "Недоступно для этой модели"
    : effectiveChecked
      ? "В контексте"
      : "Исключить из контекста";
  return (
    <button
      type="button"
      onClick={disabled ? undefined : onToggle}
      disabled={disabled}
      title={label}
      aria-label={label}
      aria-pressed={effectiveChecked}
      className={`absolute bottom-1 right-1 flex h-[18px] w-[18px] items-center justify-center rounded transition-all duration-150 ${
        effectiveChecked
          ? "bg-emerald-500 text-white"
          : "border border-neutral-500 bg-black/60 text-transparent"
      } ${disabled ? "cursor-not-allowed opacity-60" : ""}`}
    >
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path
          d="M5 13l4 4L19 7"
          stroke="currentColor"
          strokeWidth="3"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </button>
  );
}

function PersistedImage({
  image,
  onToggle,
  onOpen,
  modelSupportsImages = true,
}: {
  image: ContextImage;
  onToggle: () => void;
  onOpen: () => void;
  modelSupportsImages?: boolean;
}) {
  const src = `data:${image.media_type};base64,${image.full_base64}`;
  // A text-only model can't see the image, so it's effectively out of context
  // regardless of the stored flag (tr-hwa8).
  const inContext = image.includedInContext && modelSupportsImages;
  return (
    <div className="relative h-24 w-24">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={src}
        alt="Вложение"
        onClick={onOpen}
        className={`h-24 w-24 cursor-pointer rounded-lg object-cover transition-opacity duration-150 ${
          inContext ? "" : "opacity-50"
        }`}
      />
      <ContextCheckbox
        checked={image.includedInContext}
        onToggle={onToggle}
        disabled={!modelSupportsImages}
        disabledTitle="Модель не поддерживает изображения — оно не попадёт в контекст"
      />
    </div>
  );
}

function FileGlyph() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M13 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9l-6-6z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path d="M13 3v6h6" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

// A clickable file chip in chat history. Clicking opens the viewer (PDF/text) or
// downloads (other kinds). Persisted chips also carry a context checkbox so the
// file can be toggled in/out of the model context, mirroring image thumbnails.
function HistoryFileChip({
  filename,
  sizeBytes,
  kind,
  dimmed,
  onOpen,
  checkbox,
}: {
  filename: string;
  sizeBytes: number;
  kind: FileKind;
  dimmed?: boolean;
  onOpen: () => void;
  checkbox?: React.ReactNode;
}) {
  const actionHint = isViewableKind(kind) ? "Открыть" : "Скачать";
  return (
    <div className="relative">
      <button
        type="button"
        onClick={onOpen}
        title={`${filename} — ${actionHint}`}
        className={`flex max-w-[220px] items-center gap-2 rounded-lg border border-neutral-700 bg-neutral-800 px-3 py-2 text-left transition-colors hover:border-neutral-600 hover:bg-neutral-700 ${
          checkbox ? "pr-7" : ""
        } ${dimmed ? "opacity-50" : ""}`}
      >
        <span className="shrink-0 text-neutral-400">
          <FileGlyph />
        </span>
        <span className="min-w-0">
          <span className="block truncate text-xs font-medium text-neutral-200">
            {filename}
          </span>
          <span className="block font-mono text-[10px] uppercase tracking-wide text-neutral-500">
            {kind} · {formatFileSize(sizeBytes)}
          </span>
        </span>
      </button>
      {checkbox}
    </div>
  );
}

// A history thumbnail for an image the user attached. previewUrl is an object
// URL that is revoked when the chat window unmounts (and gone after a reload,
// since images aren't persisted) — fall back to a placeholder when it fails.
function HistoryImage({
  previewUrl,
  onOpen,
}: {
  previewUrl: string;
  onOpen: () => void;
}) {
  const [broken, setBroken] = useState(false);
  if (broken) {
    return (
      <div
        title="Изображение недоступно"
        className="flex h-24 w-24 items-center justify-center rounded-lg border border-neutral-700 bg-neutral-800 text-neutral-500"
      >
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <rect x="3" y="3" width="18" height="18" rx="2" stroke="currentColor" strokeWidth="2" />
          <path d="M3 16l5-5 4 4 3-3 6 6" stroke="currentColor" strokeWidth="2" />
        </svg>
      </div>
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={previewUrl}
      alt="Вложение"
      onClick={onOpen}
      onError={() => setBroken(true)}
      className="max-h-[200px] max-w-[200px] cursor-pointer rounded-lg object-cover"
    />
  );
}

// Copy the full assistant response to the clipboard. Subtle icon that confirms
// with a brief checkmark.
function CopyResponse({ content }: { content: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard unavailable (insecure context) — silently ignore.
    }
  };
  return (
    <Tooltip label={copied ? "Скопировано" : "Скопировать ответ"}>
      <button
        type="button"
        onClick={onCopy}
        aria-label="Скопировать ответ"
        className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-neutral-500 outline-none transition-colors hover:bg-neutral-700 hover:text-neutral-200 focus-visible:text-neutral-200 active:scale-95"
      >
        {copied ? (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path
              d="M5 13l4 4L19 7"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        ) : (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <rect x="9" y="9" width="11" height="11" rx="2" stroke="currentColor" strokeWidth="1.5" />
            <path d="M5 15V5a2 2 0 0 1 2-2h10" stroke="currentColor" strokeWidth="1.5" />
          </svg>
        )}
      </button>
    </Tooltip>
  );
}

// Small, unobtrusive copy-request-id control (FEATURE 1).
function CopyRequestId({ requestId }: { requestId: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(requestId);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard unavailable (insecure context) — silently ignore.
    }
  };
  return (
    <Tooltip label={copied ? "Скопировано" : "Скопировать ID запроса"}>
      <button
        type="button"
        onClick={onCopy}
        aria-label="Скопировать ID запроса"
        className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-neutral-500 outline-none transition-colors hover:bg-neutral-700 hover:text-neutral-300 focus-visible:text-neutral-300"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <rect x="9" y="9" width="11" height="11" rx="2" stroke="currentColor" strokeWidth="1.5" />
          <path d="M5 15V5a2 2 0 0 1 2-2h10" stroke="currentColor" strokeWidth="1.5" />
        </svg>
        <span className="font-mono">{copied ? "Скопировано" : requestId.slice(0, 8)}</span>
      </button>
    </Tooltip>
  );
}

// ── Meta-row icons (gray, tooltip-backed) ───────────────────────────────────
// The assistant meta row reads as a quiet strip of monochrome glyphs; each one
// reveals its figure in a hover tooltip so the row never competes with the
// answer. All icons inherit the row's neutral-500 and brighten on hover.

// Ruble (₽) — cost of this answer.
function RubleIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M8 13V4h4.5a4.5 4.5 0 0 1 0 9H8m0 0v7M5 16.5h7"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// Down/up arrows — input vs output tokens.
function InOutIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M8 4v16m0 0-3-3m3 3 3-3M16 20V4m0 0-3 3m3-3 3 3"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// Spark — extended-thinking tokens (mirrors the ModelMenu thinking glyph).
function ThinkingSparkIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M12 3l1.8 4.7L18.5 9.5 13.8 11.3 12 16l-1.8-4.7L5.5 9.5l4.7-1.8L12 3z"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// Globe — web search was used for this answer.
function GlobeIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.4" />
      <path
        d="M3 12h18M12 3c2.5 2.4 3.8 5.6 3.8 9S14.5 18.6 12 21M12 3C9.5 5.4 8.2 8.6 8.2 12S9.5 18.6 12 21"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// One quiet, tooltip-backed meta glyph. `aria-label` doubles as the accessible
// name since the visible figure now lives in the hover tooltip.
function MetaIcon({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <Tooltip label={label}>
      <span
        tabIndex={0}
        aria-label={label}
        className="inline-flex items-center rounded-md px-2 py-1 text-neutral-500 outline-none transition-colors hover:bg-neutral-700 hover:text-neutral-300 focus-visible:text-neutral-300"
      >
        {children}
      </span>
    </Tooltip>
  );
}

function MessageBubbleInner({
  message,
  isLast,
  images,
  files,
  precedingHistoryTokens,
  onToggleImageContext,
  onToggleFileContext,
  conversationVersions,
  onLoadVersions,
  onSwitchVersion,
  onRegenerate,
  onEditAndResend,
  onResend,
  selectedModel,
  onOpenArtifact,
  conversationId,
}: MessageBubbleProps) {
  const isUser = message.role === "user";
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);
  const [fileTarget, setFileTarget] = useState<FileTarget | null>(null);
  // Inline edit state for a user turn (text-only edit → editAndResend).
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(message.content);

  // tr-hwa8: an attachment is only really in context if the active model can
  // consume its type; otherwise the "в контексте" toggle would lie about what the
  // model receives. Default true when the model is unknown so we never over-restrict.
  const modelSupportsImages = selectedModel?.supports_image_input ?? true;
  const modelSupportsFiles = selectedModel?.supports_file_input ?? true;

  const versionCount = message.versionCount ?? 1;
  const isBranched = versionCount > 1;
  // A switcher is showing but its sibling bucket isn't cached yet — pull it once
  // so the chevrons resolve to real neighbor ids. onLoadVersions is idempotent
  // (it re-fetches the whole sibling map), so the parent dedupes repeats.
  useEffect(() => {
    if (
      isBranched &&
      onLoadVersions &&
      !conversationVersions?.get(message.parentId ?? "")
    ) {
      onLoadVersions();
    }
  }, [isBranched, message.parentId, onLoadVersions, conversationVersions]);

  const { prevId, nextId } = siblingNeighbors(message, conversationVersions);
  // Render a switcher for this turn. Chevrons page to the resolved sibling node
  // (no-op until the cache loads). index is 0-based (version_index is 1-based).
  const switcher = isBranched ? (
    <VersionSwitcher
      index={(message.versionIndex ?? 1) - 1}
      count={versionCount}
      onPrev={() => {
        if (prevId) onSwitchVersion?.(prevId);
      }}
      onNext={() => {
        if (nextId) onSwitchVersion?.(nextId);
      }}
    />
  ) : null;

  // Date-if-not-today for the user hover row: omit "сегодня", show the short RU
  // relative/date label otherwise (§spec — today is implied, so it's dropped).
  const relativeDate = message.createdAt
    ? formatRelativeDate(message.createdAt)
    : "";
  const dateLabel = relativeDate && relativeDate !== "сегодня" ? relativeDate : "";
  // The user bubble can be copied just like the assistant answer.
  const canCopyUser = !!message.content;

  // Viewable files open the modal; everything else downloads directly.
  const openFile = (target: FileTarget) => {
    if (isViewableKind(target.kind)) setFileTarget(target);
    else void downloadFile(target);
  };

  if (isUser) {
    // Prefer persisted images (with context checkboxes) once loaded; otherwise
    // fall back to the local previews of a just-sent message (no checkbox).
    const hasPersisted = (images?.length ?? 0) > 0;
    const hasLocal = (message.attachedImages?.length ?? 0) > 0;
    const hasPersistedFiles = (files?.length ?? 0) > 0;
    // Full-resolution sources for the lightbox, in display order.
    const lightboxSrcs = hasPersisted
      ? images!.map((img) => `data:${img.media_type};base64,${img.full_base64}`)
      : hasLocal
        ? message.attachedImages!.map((img) => img.previewUrl)
        : [];
    return (
      <div className="flex animate-slideUp justify-end">
        <div className="group/umsg flex min-w-0 max-w-[80%] flex-col items-end gap-1.5">
          {hasPersisted ? (
            <div className="flex flex-wrap justify-end gap-2">
              {images!.map((img, idx) => (
                <PersistedImage
                  key={img.id}
                  image={img}
                  onToggle={() => onToggleImageContext?.(img.id)}
                  onOpen={() => setLightboxIndex(idx)}
                  modelSupportsImages={modelSupportsImages}
                />
              ))}
            </div>
          ) : hasLocal ? (
            <div className="flex flex-wrap justify-end gap-2">
              {message.attachedImages!.map((img, idx) => (
                <HistoryImage
                  key={idx}
                  previewUrl={img.previewUrl}
                  onOpen={() => setLightboxIndex(idx)}
                />
              ))}
            </div>
          ) : null}
          {hasPersistedFiles ? (
            <div className="flex flex-wrap justify-end gap-2">
              {files!.map((f) => (
                <HistoryFileChip
                  key={f.id}
                  filename={f.filename}
                  sizeBytes={f.file_size_bytes}
                  kind={f.kind}
                  dimmed={!(f.includedInContext && modelSupportsFiles)}
                  onOpen={() =>
                    openFile({
                      source: "persisted",
                      fileId: f.id,
                      filename: f.filename,
                      kind: f.kind,
                      mediaType: f.media_type,
                    })
                  }
                  checkbox={
                    <ContextCheckbox
                      checked={f.includedInContext}
                      onToggle={() => onToggleFileContext?.(f.id)}
                      disabled={!modelSupportsFiles}
                      disabledTitle="Модель не поддерживает файлы — он не попадёт в контекст"
                    />
                  }
                />
              ))}
            </div>
          ) : message.attachedFiles?.length ? (
            <div className="flex flex-wrap justify-end gap-2">
              {message.attachedFiles.map((f, idx) => (
                <HistoryFileChip
                  key={idx}
                  filename={f.filename}
                  sizeBytes={f.sizeBytes}
                  kind={f.kind}
                  onOpen={() =>
                    openFile({
                      source: "local",
                      blobUrl: f.blobUrl,
                      filename: f.filename,
                      kind: f.kind,
                      mediaType: f.mediaType,
                    })
                  }
                />
              ))}
            </div>
          ) : null}
          {editing ? (
            <div className="flex w-full min-w-[320px] flex-col items-end gap-1.5">
              <textarea
                autoFocus
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                rows={Math.min(8, Math.max(2, draft.split("\n").length))}
                className="w-full min-w-[320px] resize-none rounded-2xl rounded-br-sm border border-neutral-700 bg-neutral-900 px-4 py-2 text-sm text-neutral-100 outline-none focus:border-emerald-600"
              />
              {(() => {
                const p = estimatePrognosis({
                  historyTokens: precedingHistoryTokens ?? 0,
                  text: draft,
                  images: (images ?? []).filter((i) => i.includedInContext)
                    .length,
                  files: (files ?? [])
                    .filter((f) => f.includedInContext)
                    .map((f) => ({ tokenEstimate: f.token_estimate })),
                  model: selectedModel,
                });
                return (
                  <span className="px-2 font-mono text-[11px] tabular-nums text-neutral-500">
                    ~{formatTokenCount(p.inputTokens)} токенов · ~
                    {p.costRub.toFixed(4)} ₽
                  </span>
                );
              })()}
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setEditing(false);
                    setDraft(message.content);
                  }}
                  className="rounded-lg px-3 py-1 text-xs text-neutral-400 transition-colors hover:text-neutral-200"
                >
                  Отмена
                </button>
                <button
                  type="button"
                  disabled={
                    draft.trim().length === 0 || draft === message.content
                  }
                  onClick={() => {
                    setEditing(false);
                    onEditAndResend?.(message.id, draft.trim());
                  }}
                  className="rounded-lg bg-emerald-600 px-3 py-1 text-xs text-white transition-colors hover:bg-emerald-500 disabled:cursor-not-allowed disabled:bg-emerald-600/40"
                >
                  Сохранить
                </button>
              </div>
            </div>
          ) : message.content ? (
            <div className="min-w-0 rounded-2xl rounded-br-sm border border-neutral-700 bg-neutral-800 px-4 py-2 text-sm text-neutral-100">
              <ClampedContent clampPx={200} fadeClass="from-neutral-800">
                <div className="whitespace-pre-wrap break-words">
                  {message.content}
                </div>
              </ClampedContent>
            </div>
          ) : null}

          {/* User hover meta row — Изменить / Отправить заново / Скопировать /
              date-if-not-today + version switcher. Hover-gated exactly like the
              assistant meta row (opacity-0 … group-hover/umsg:opacity-100).
              Hidden while editing so the controls don't fight the textarea. */}
          {!editing &&
          (onEditAndResend || onResend || canCopyUser || isBranched) ? (
            <div className="flex items-center gap-1.5 px-2 font-mono text-xs text-neutral-500 opacity-0 transition-opacity duration-200 focus-within:opacity-100 group-hover/umsg:opacity-100">
              {dateLabel ? (
                <span className="text-neutral-600">{dateLabel}</span>
              ) : null}
              {onResend ? (
                <button
                  type="button"
                  onClick={() => onResend(message.id)}
                  title="Отправить заново"
                  aria-label="Отправить сообщение заново"
                  className="inline-flex rounded-md px-2 py-1 transition-colors hover:bg-neutral-700 hover:text-neutral-200"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                    <path
                      d="M4 12a8 8 0 0 1 13.7-5.7L20 8M20 4v4h-4M20 12a8 8 0 0 1-13.7 5.7L4 16M4 20v-4h4"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </button>
              ) : null}
              {onEditAndResend ? (
                <button
                  type="button"
                  onClick={() => {
                    setDraft(message.content);
                    setEditing(true);
                  }}
                  title="Изменить"
                  aria-label="Изменить сообщение"
                  className="inline-flex rounded-md px-2 py-1 transition-colors hover:bg-neutral-700 hover:text-neutral-200"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                    <path
                      d="M4 20h4L17.5 10.5l-4-4L4 16v4z"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                    <path
                      d="M12.5 7.5l4 4"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinecap="round"
                    />
                  </svg>
                </button>
              ) : null}
              {canCopyUser ? <CopyResponse content={message.content} /> : null}
              {switcher}
            </div>
          ) : null}
        </div>
        <Lightbox
          images={lightboxSrcs}
          index={lightboxIndex}
          onClose={() => setLightboxIndex(null)}
        />
        {fileTarget ? (
          <FileViewerModal target={fileTarget} onClose={() => setFileTarget(null)} />
        ) : null}
      </div>
    );
  }

  const canCopy = !message.errorMessage && !message.isStreaming && !!message.content;

  // A stopped reply surfaces «Остановлено» on the thinking timeline's terminal
  // node — but a thinking-OFF content stop renders NO timeline, so the signal
  // would be lost. In that case show a small inline marker on the meta row
  // instead, so a reloaded stopped reply is always distinguishable (tr-fo1).
  const hasThinkingTimeline =
    !!message.thinking ||
    (message.thinkingSegments?.length ?? 0) > 0 ||
    (message.thinkingSteps?.length ?? 0) > 0;
  const showInlineStopped =
    !!message.stopped && !message.errorMessage && !hasThinkingTimeline;

  // Model-authored file artifacts derived from the persisted tool steps — one
  // downloadable card per logical file, shown under the answer (tr-cy0.2.13).
  const toolArtifacts = deriveToolArtifacts(message.thinkingSteps);

  // Lift any large generated document/code block out of the answer into a card
  // (+ canvas). Runs during streaming too: an unclosed fence parses to EOF, so
  // the card forms live in a «Генерация…» state and finalizes on done. Skip the
  // heuristic when real (server) tool artifacts already exist for this message,
  // so the light path never stacks a phantom card over the tool card (tr-v0l).
  const { stripped: answerText, artifacts } = message.content
    ? extractArtifacts(message.content, { skip: toolArtifacts.length > 0 })
    : { stripped: "", artifacts: [] as Artifact[] };

  return (
    <div className="flex animate-slideUp justify-start">
      <div className="group/msg flex w-full min-w-0 max-w-[90%] flex-col gap-1">
        {message.contextTruncated ? (
          <div className="rounded border border-amber-800/50 bg-amber-950/40 px-3 py-2 text-xs text-amber-300">
            ⚠ Часть истории диалога была сокращена из-за ограничений модели.
          </div>
        ) : null}

        <ThinkingProcess process={buildThinkingProcess(message)} />

        {/* The answer renders borderless, directly on the canvas (Claude
            aesthetic) — no bubble, no divider. Errors keep a small red panel for
            legibility. Nothing renders while only thinking is streaming. */}
        {message.errorMessage ? (
          <div className="min-w-0 rounded-lg border border-red-900/50 bg-red-950/30 px-4 py-2 text-sm text-red-400">
            {message.errorMessage}
          </div>
        ) : message.content || (message.isStreaming && !message.isThinking) ? (
          // While streaming with no content yet, render nothing here: the
          // animated Trinity neural mark below is the sole loading indicator
          // (no redundant three-dot ellipsis).
          message.isStreaming && !message.content ? null : (
            <div className="min-w-0">
              <RevealMarkdown
                content={answerText || ""}
                streaming={!!message.isStreaming}
              />
              {artifacts.map((artifact, i) => (
                <ArtifactCard
                  // Content-hash id is stable once settled; while streaming the
                  // content changes each frame, so key by position to keep the
                  // card mounted (no per-token remount/flicker).
                  key={message.isStreaming ? `artifact-${i}` : artifact.id}
                  artifact={artifact}
                  generating={message.isStreaming}
                  onOpen={() => onOpenArtifact?.(artifact)}
                  onDownload={() => downloadArtifact(artifact)}
                />
              ))}
            </div>
          )
        ) : null}

        {/* Model-authored file artifact cards — one per logical file, between
            the answer and the action row (reference screenshot_2). */}
        {!message.errorMessage && toolArtifacts.length > 0
          ? toolArtifacts.map((artifact) => (
              <ToolArtifactCard
                key={artifact.filename}
                artifact={artifact}
                conversationId={conversationId}
                onOpenArtifact={onOpenArtifact}
              />
            ))
          : null}

        {/* Inline «Остановлено» marker for a thinking-OFF content stop, where no
            thinking timeline renders to carry the terminal node (tr-fo1). Always
            visible (not hover-gated like the controls below) so the stopped signal
            is never lost. Calm amber, never error red — interrupted, not failed. */}
        {showInlineStopped ? (
          <div className="flex items-center gap-1 px-2 font-mono text-xs text-amber-400">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.6" />
              <rect x="9" y="9" width="6" height="6" rx="1" fill="currentColor" />
            </svg>
            Остановлено
          </div>
        ) : null}

        {!message.errorMessage &&
        (canCopy ||
          message.actualCost ||
          message.requestId ||
          message.webSearchEnabled ||
          (onRegenerate && !message.isStreaming) ||
          isBranched) ? (
          <div
            className={`flex items-center gap-1.5 px-2 font-mono text-xs text-neutral-500 transition-opacity duration-200 ${
              isLast
                ? "opacity-100"
                : "opacity-0 focus-within:opacity-100 group-hover/msg:opacity-100"
            }`}
          >
            {canCopy ? <CopyResponse content={message.content} /> : null}
            {onRegenerate && !message.isStreaming ? (
              <Tooltip label="Создать ответ заново">
                <button
                  type="button"
                  onClick={() => onRegenerate(message.id)}
                  aria-label="Создать ответ заново"
                  className="inline-flex items-center rounded-md px-2 py-1 text-neutral-500 outline-none transition-colors hover:bg-neutral-700 hover:text-neutral-200 focus-visible:text-neutral-200 active:scale-95"
                >
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                    <path
                      d="M4 12a8 8 0 0 1 13.7-5.7L20 8M20 4v4h-4M20 12a8 8 0 0 1-13.7 5.7L4 16M4 20v-4h4"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </button>
              </Tooltip>
            ) : null}
            {switcher}
            {isBranched && message.modelId ? (
              <Tooltip label="Модель этой версии">
                <span className="text-neutral-600">
                  {formatModelName(message.modelId)}
                </span>
              </Tooltip>
            ) : null}
            {message.actualCost ? (
              <MetaIcon label={`Стоимость ответа: ${formatCost(message.actualCost)}`}>
                <RubleIcon />
              </MetaIcon>
            ) : null}
            {message.tokensUsed ? (
              <MetaIcon
                label={`Токены: ${formatTokenCount(
                  message.tokensUsed.input,
                )} входных · ${formatTokenCount(
                  message.tokensUsed.output,
                )} выходных`}
              >
                <InOutIcon />
              </MetaIcon>
            ) : null}
            {message.reasoningTokens ? (
              <MetaIcon
                label={`Токены мышления: ${formatTokenCount(
                  message.reasoningTokens,
                )}`}
              >
                <ThinkingSparkIcon />
              </MetaIcon>
            ) : null}
            {message.webSearchEnabled ? (
              <MetaIcon label="Использован веб-поиск">
                <GlobeIcon />
              </MetaIcon>
            ) : null}
            {message.requestId && !message.isStreaming ? (
              <span className="ml-auto">
                <CopyRequestId requestId={message.requestId} />
              </span>
            ) : null}
          </div>
        ) : null}

        {/* The neural mark anchors the last assistant turn — tucked flush-left
            directly under the meta row so it reads as part of that bubble, not
            a floating ornament. Past turns omit it so the canvas stays clean.
            Streaming intensifies the animation; hover does the same on demand. */}
        {isLast && !message.errorMessage ? (
          <div className="mt-1 pl-0.5">
            <NeuralIcon size={72} isStreaming={message.isStreaming} />
          </div>
        ) : null}
      </div>
    </div>
  );
}

// Content-compare the live timeline arrays. useChat snapshots a FRESH
// thinkingSteps / thinkingSegments array on every ~50ms flush (map(s => ({...s}))),
// so a reference compare reported "changed" 20–40×/sec even when nothing did —
// re-rendering the whole bubble each flush (audit H7). thinkingSegments was also
// missing from the comparator entirely, so segment-only updates were SKIPPED.
function thinkingStepsEqual(
  a: ChatMessage["thinkingSteps"],
  b: ChatMessage["thinkingSteps"],
): boolean {
  if (a === b) return true;
  if (!a || !b || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    const x = a[i];
    const y = b[i];
    if (
      x.id !== y.id ||
      x.status !== y.status ||
      x.title !== y.title ||
      x.filename !== y.filename ||
      x.presented !== y.presented ||
      x.output !== y.output ||
      x.input !== y.input ||
      x.diff?.added !== y.diff?.added ||
      x.diff?.removed !== y.diff?.removed
    ) {
      return false;
    }
  }
  return true;
}

function thinkingSegmentsEqual(
  a: ChatMessage["thinkingSegments"],
  b: ChatMessage["thinkingSegments"],
): boolean {
  if (a === b) return true;
  if (!a || !b || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    const x = a[i];
    const y = b[i];
    if (
      x.id !== y.id ||
      x.text !== y.text ||
      x.summary !== y.summary ||
      x.summaryPending !== y.summaryPending
    ) {
      return false;
    }
  }
  return true;
}

// React.memo comparator (exported for unit tests). Only inspects the fields
// rendered above. The `message` object reference changes whenever useChat's
// messages array is updated (every content_delta), but most rows are untouched —
// the comparator skips re-rendering them. The live arrays are compared by content
// (H7) so an unchanged snapshot doesn't force a re-render.
export function messagePropsEqual(
  prev: MessageBubbleProps,
  next: MessageBubbleProps,
): boolean {
  const a = prev.message;
  const b = next.message;
  if (prev.isLast !== next.isLast) return false;
  if (prev.images !== next.images) return false;
  if (prev.files !== next.files) return false;
  if (prev.precedingHistoryTokens !== next.precedingHistoryTokens) return false;
  if (prev.onToggleImageContext !== next.onToggleImageContext) return false;
  if (prev.onToggleFileContext !== next.onToggleFileContext) return false;
  if (prev.conversationVersions !== next.conversationVersions) return false;
  if (prev.onLoadVersions !== next.onLoadVersions) return false;
  if (prev.onSwitchVersion !== next.onSwitchVersion) return false;
  if (prev.onRegenerate !== next.onRegenerate) return false;
  if (prev.onEditAndResend !== next.onEditAndResend) return false;
  if (prev.onResend !== next.onResend) return false;
  if (prev.selectedModel !== next.selectedModel) return false;
  if (prev.onOpenArtifact !== next.onOpenArtifact) return false;
  if (prev.conversationId !== next.conversationId) return false;
  return (
    a === b ||
    (a.id === b.id &&
      a.role === b.role &&
      a.content === b.content &&
      a.isStreaming === b.isStreaming &&
      a.thinking === b.thinking &&
      a.isThinking === b.isThinking &&
      a.reasoningTokens === b.reasoningTokens &&
      a.thinkingStartedAt === b.thinkingStartedAt &&
      a.thinkingDurationMs === b.thinkingDurationMs &&
      thinkingStepsEqual(a.thinkingSteps, b.thinkingSteps) &&
      thinkingSegmentsEqual(a.thinkingSegments, b.thinkingSegments) &&
      a.actualCost === b.actualCost &&
      a.errorMessage === b.errorMessage &&
      a.contextTruncated === b.contextTruncated &&
      a.requestId === b.requestId &&
      a.webSearchEnabled === b.webSearchEnabled &&
      a.stopped === b.stopped &&
      a.parentId === b.parentId &&
      a.isActive === b.isActive &&
      a.versionIndex === b.versionIndex &&
      a.versionCount === b.versionCount &&
      a.modelId === b.modelId &&
      a.createdAt === b.createdAt &&
      a.attachedImages === b.attachedImages &&
      a.attachedFiles === b.attachedFiles &&
      a.tokensUsed?.input === b.tokensUsed?.input &&
      a.tokensUsed?.output === b.tokensUsed?.output)
  );
}

export const MessageBubble = memo(MessageBubbleInner, messagePropsEqual);

MessageBubble.displayName = "MessageBubble";

export default MessageBubble;
