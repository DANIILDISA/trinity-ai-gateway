"use client";

import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { NeuralIcon } from "@/components/ui/NeuralIcon";
import { PaperclipIcon } from "@/components/ui/PaperclipIcon";
import { ToggleVisual } from "@/components/ui/ToggleSwitch";
import { pickGreeting } from "@/lib/greetings";
import { ModelSelector } from "./ModelSelector";
import { ModelMenu } from "./ModelMenu";
import { MessageBubble } from "./MessageBubble";
import { ChatFeedSkeleton } from "./ChatFeedSkeleton";
import { MicroAnalytics } from "./MicroAnalytics";
import { ArtifactPanel, type ArtifactView } from "./ArtifactPanel";
import { useStickToBottom } from "./streaming/useStickToBottom";
import { useScrollRestoration } from "./streaming/useScrollRestoration";
import type { Artifact } from "@/lib/artifacts";
import { fetchToolArtifact } from "@/lib/downloadToolArtifact";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import type { AttachedFile, AttachedImage, ChatMessage } from "@/hooks/useChat";
import type {
  BranchTotals,
  ContextFile,
  ContextImage,
  MessageResponse,
  ModelInfo,
  ThinkingEffort,
} from "@/types/api";
import {
  MAX_ATTACHMENTS,
  MAX_FILE_BYTES,
  MAX_IMAGE_BYTES,
  buildAccept,
  classifyFile,
  estimateDocxTokens,
  estimatePdfTokens,
  estimateTextTokens,
  formatFileSize,
  isSupportedFile,
} from "@/lib/fileTypes";

// Per-image token estimate (mirrors backend _IMAGE_TOKEN_ESTIMATE); used for the
// composer's context-window warning.
const IMAGE_TOKENS = 1000;
const CHARS_PER_TOKEN = 4;

// Hard cap on a single message's text length. Mirrors the backend
// `_MAX_TEXT_LEN = 32000` in `backend/app/schemas/chat.py` — keep the two in
// sync. The composer blocks sending past this so the user learns before the
// request is fired, rather than only on the server's 422.
export const MAX_MESSAGE_CHARS = 32000;

// Accepted image MIME types (the existing image-attachment contract, unchanged).
const ACCEPTED_IMAGE_TYPES = [
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/gif",
];

// Deterministic per-hour seed: same value for every reload within one clock hour,
// changes when the hour (or day) rolls over.
function hourSeed(d: Date): number {
  return (((d.getFullYear() * 13 + (d.getMonth() + 1)) * 32 + d.getDate()) * 25 + d.getHours()) >>> 0;
}
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// useLayoutEffect commits synchronously before the browser paints, but React
// warns it does nothing during SSR. Degrade to the passive effect on the server
// (where it's a no-op anyway) and use the real layout effect in the browser.
const useIsomorphicLayoutEffect =
  typeof window !== "undefined" ? useLayoutEffect : useEffect;

function attachmentId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `att-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

// Base64-encode raw bytes in chunks (avoids call-stack overflow on big files).
function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

function PlusIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

function GlobeIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.5" />
      <path
        d="M3 12h18M12 3c2.5 2.5 2.5 15 0 18M12 3c-2.5 2.5-2.5 15 0 18"
        stroke="currentColor"
        strokeWidth="1.5"
      />
    </svg>
  );
}

function FileIcon() {
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

function ChartIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M3 3v18h18" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <rect x="7" y="11" width="3" height="6" stroke="currentColor" strokeWidth="1.5" />
      <rect x="13" y="7" width="3" height="10" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

function SendIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M12 19V5M5 12l7-7 7 7"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export interface ConversationMenuActions {
  isPinned: boolean;
  onTogglePin: () => void;
  onRename: (title: string) => void;
  onDelete: () => void;
}

export interface ChatWindowProps {
  messages: ChatMessage[];
  isStreaming: boolean;
  error: string | null;
  models: ModelInfo[];
  selectedModelId: string;
  inputValue: string;
  isLoadingHistory: boolean;
  webSearchEnabled: boolean;
  thinkingEnabled: boolean;
  thinkingEffort: ThinkingEffort;
  toolsEnabled: boolean;
  conversationImages: Map<string, ContextImage[]>;
  conversationFiles: Map<string, ContextFile[]>;
  // Version history (tr-x81 / §6). Sibling cache keyed by parent_id + the
  // branching actions, threaded into each MessageBubble for the VersionSwitcher
  // and the regenerate/edit/resend controls. All optional so the new-chat page
  // (no branches yet) can omit them.
  conversationVersions?: Map<string, MessageResponse[]>;
  onLoadVersions?: () => void;
  onSwitchVersion?: (nodeId: string) => void;
  onRegenerate?: (assistantId: string, opts?: { modelId?: string }) => void;
  onEditAndResend?: (userId: string, content: string) => void;
  onResend?: (userId: string) => void;
  // All-branch aggregate from the loaded conversation (Task 13). Forwarded to
  // MicroAnalytics so it can render the «Все ветки» section when branched.
  branchTotals?: BranchTotals;
  sidebarOpen: boolean;
  // Active conversation id — threaded into each MessageBubble so a tool-artifact
  // card can download its file from the per-conversation endpoint (tr-cy0.2.13).
  conversationId?: string;
  conversationTitle?: string | null;
  conversationMenu?: ConversationMenuActions | null;
  /** Authenticated user's display name. Used in the empty-state greeting. */
  userName?: string | null;
  onToggleImageContext: (imageId: string) => void;
  onToggleFileContext: (fileId: string) => void;
  onModelChange: (id: string) => void;
  onInputChange: (value: string) => void;
  onSend: (images: AttachedImage[], files: AttachedFile[]) => void;
  onStop: () => void;
  onClearError: () => void;
  onToggleWebSearch: () => void;
  onToggleThinking: () => void;
  onSetThinkingEffort: (effort: ThinkingEffort) => void;
  onToggleTools: () => void;
}

// Composer textarea may grow up to half the viewport so long prompts are
// comfortable to write; the CSS `max-h-[50vh]` class is the visual ceiling,
// and the auto-resize effect mirrors that height in JS so the rendered box
// matches scrollHeight without exceeding the cap.
const TEXTAREA_MAX_VH_FRACTION = 0.5;
// Resting/minimum composer height. A single `text-sm leading-6` row (~24px) plus
// the textarea's symmetric `py-2.5` padding (~10px top + ~10px bottom) renders at
// ~44px — the placeholder sits balanced (equal air above the text and below it
// toward the "+" row), matching the reference composer (tr-cy0.6.15). This floor
// mirrors the `min-h-[44px]` class so the auto-resize JS never shrinks the box
// below the resting height.
const TEXTAREA_MIN_PX = 44;

function ChatWindowInner({
  messages,
  isStreaming,
  error,
  models,
  selectedModelId,
  inputValue,
  isLoadingHistory,
  webSearchEnabled,
  thinkingEnabled,
  toolsEnabled,
  thinkingEffort,
  conversationImages,
  conversationFiles,
  conversationVersions,
  onLoadVersions,
  onSwitchVersion,
  onRegenerate,
  onEditAndResend,
  onResend,
  branchTotals,
  sidebarOpen,
  conversationId,
  conversationTitle,
  conversationMenu,
  userName,
  onToggleImageContext,
  onToggleFileContext,
  onModelChange,
  onInputChange,
  onSend,
  onStop,
  onClearError,
  onToggleWebSearch,
  onToggleThinking,
  onToggleTools,
  onSetThinkingEffort,
}: ChatWindowProps) {
  const listRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const [attachedImages, setAttachedImages] = useState<AttachedImage[]>([]);
  const [attachedFiles, setAttachedFiles] = useState<AttachedFile[]>([]);
  const [imageError, setImageError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [analyticsOpen, setAnalyticsOpen] = useState(false);
  // Keeps the panel mounted through its exit animation: open flips
  // analyticsOpen→true synchronously, close flips it→false and unmounts 200ms
  // later (matching the wrapper's duration-200) so the slide-out can play.
  const [analyticsMounted, setAnalyticsMounted] = useState(false);
  // Artifact canvas: the open document, its view, and the open/mounted pair that
  // mirrors the analytics slide animation. Canvas and analytics are mutually
  // exclusive in the right zone (opening one closes the other).
  const [openArtifact, setOpenArtifact] = useState<Artifact | null>(null);
  const [artifactView, setArtifactView] = useState<ArtifactView>("preview");
  const [artifactOpen, setArtifactOpen] = useState(false);
  const [artifactMounted, setArtifactMounted] = useState(false);
  // >= lg shows the canvas as a split column; below that it's a fullscreen
  // overlay (drives the ArtifactPanel `fullscreen` behavior — trap/scroll-lock).
  const isWideCanvas = useMediaQuery("(min-width: 1024px)");
  const [plusMenuOpen, setPlusMenuOpen] = useState(false);
  // a11y (tr-h8l5): refs to the "+" trigger and its menu for keyboard handling.
  const plusButtonRef = useRef<HTMLButtonElement>(null);
  const plusMenuRef = useRef<HTMLDivElement>(null);
  const [modelModalOpen, setModelModalOpen] = useState(false);
  // Fix 1: transient amber notice shown near the model selector when a model
  // switch drops images/files from context.
  const [capabilityWarning, setCapabilityWarning] = useState<string | null>(null);
  // Per-hour empty-state greeting. Starts as "" so the server-rendered markup and
  // the first client render match (the deterministic per-hour pick / current-time
  // Date never run during render), avoiding a hydration mismatch. On the client an
  // isomorphic layout effect fills it BEFORE the browser paints, so the first
  // visible frame already shows the greeting (no empty-frame flicker); during
  // SSR/hydration it stays "" so the two render passes still match.
  const [greeting, setGreeting] = useState("");

  // Mirror current attachments into a ref so event-handler closures (paste,
  // drop, unmount cleanup) read the latest list without being dependencies.
  const imagesRef = useRef<AttachedImage[]>([]);
  imagesRef.current = attachedImages;
  const filesRef = useRef<AttachedFile[]>([]);
  filesRef.current = attachedFiles;
  const errorTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dragDepth = useRef(0);
  // tr-n5b (MONEY): synchronous in-flight submit lock. Two Enter keydowns (or a
  // fast double-click on «Отправить») dispatched in the SAME JS tick — before
  // React re-renders and disables the composer — would both reach `onSend` and
  // fire two POST /messages, double-billing the user (and, on a fresh chat,
  // spawning a duplicate conversation). A useState flag updates after the tick,
  // so it can't stop the second call; this ref is checked + set synchronously
  // inside `submit`, so the 2nd same-tick submit returns before sending. It's
  // released on the next render once the send has settled (input cleared and/or
  // streaming established / errored) — see the effect below.
  const submittingRef = useRef(false);
  // Pending unmount timeout for the analytics panel's exit animation; cleared on
  // re-open so a quick close→open doesn't unmount the freshly-shown panel.
  const analyticsCloseTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  // Pending unmount timeout for the artifact canvas's exit animation.
  const artifactCloseTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  // Element that opened the canvas (the ArtifactCard), refocused on close.
  const lastFocusedRef = useRef<HTMLElement | null>(null);

  // Fix 1: mirror the context maps into refs so the capability-change effect
  // reads the latest state without re-running on every checkbox toggle.
  const conversationImagesRef = useRef(conversationImages);
  conversationImagesRef.current = conversationImages;
  const conversationFilesRef = useRef(conversationFiles);
  conversationFilesRef.current = conversationFiles;
  // Capability the selected model had on the previous render, so we can detect
  // gain/loss transitions. null until the first model resolves.
  const prevCapsRef = useRef<{ vision: boolean; files: boolean } | null>(null);
  // Web-search support on the previous render + whether WE auto-disabled web
  // search on a capability loss, so a later switch back to a capable model can
  // restore the user's intent (mirrors the attachment auto-uncheck/restore).
  const prevWebSupportRef = useRef<boolean | null>(null);
  const autoDisabledWebRef = useRef(false);
  // IDs we auto-unchecked on a capability loss, so a later switch back to a
  // capable model restores exactly those (never re-checking the user's own
  // manual unchecks).
  const autoUncheckedImageIdsRef = useRef<string[]>([]);
  const autoUncheckedFileIdsRef = useRef<string[]>([]);
  const capabilityWarningTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );

  const selectedModel = models.find((m) => m.id === selectedModelId);
  const supportsWebSearch = selectedModel?.supports_web_search ?? false;
  // Web search is real only when the current model supports it. A stale "on"
  // carried over from a previous model must never leak into the hint, the toggle,
  // the request, or the result meta — the effect below also forces the underlying
  // state off so the send path (which reads the raw flag) stays correct too.
  const effectiveWebSearch = webSearchEnabled && supportsWebSearch;
  const supportsVision = selectedModel?.supports_image_input ?? false;
  const supportsFiles = selectedModel?.supports_file_input ?? false;
  const canAttach = supportsVision || supportsFiles;

  // The scrolling feed only renders once history is loaded AND there's at least
  // one message (otherwise the empty/welcome state shows and listRef is unmounted).
  // Both scroll hooks key their listener attachment on this so they (re)attach when
  // the feed mounts — which often happens after the first render (tr-hsg.9/.10).
  const feedReady = !isLoadingHistory && messages.length > 0;

  const { showScrollButton, notifyGrowth, scrollToBottom, setSticky } =
    useStickToBottom(listRef, feedReady);

  // A fresh conversation starts pinned; clears any detached state carried over
  // from the previous one. Restoration + the scroll listener re-adjust from the
  // restored position right after (tr-hsg.9/.10 follow-up).
  useEffect(() => {
    setSticky(true);
  }, [conversationId, setSticky]);

  const { restored } = useScrollRestoration(
    listRef,
    conversationId ?? "",
    feedReady,
  );

  const [showSkeleton, setShowSkeleton] = useState(false);
  useEffect(() => {
    if (!isLoadingHistory) {
      setShowSkeleton(false);
      return;
    }
    const t = setTimeout(() => setShowSkeleton(true), 200);
    return () => clearTimeout(t);
  }, [isLoadingHistory]);

  // Re-pin to the bottom only when content actually grew (cheap primitives so
  // this doesn't re-run on unrelated parent re-renders like balance polls).
  const lastMessageContentLen =
    messages.length > 0 ? messages[messages.length - 1].content.length : 0;
  useEffect(() => {
    if (!restored.current) return;
    notifyGrowth();
  }, [messages.length, lastMessageContentLen, notifyGrowth, restored]);

  // Smoothly follow the bottom every frame while generating (and for a short tail
  // after, covering the post-completion reveal drain). The text reveal grows the
  // rendered height between SSE deltas, so pinning only on deltas drifts/jitters;
  // a per-frame pin makes the scroll glide with the streaming text. notifyGrowth
  // is a no-op once the user scrolls up (sticky=false), so this never traps them.
  const [followBottom, setFollowBottom] = useState(false);
  useEffect(() => {
    if (isStreaming) {
      setFollowBottom(true);
      return;
    }
    const t = setTimeout(() => setFollowBottom(false), 900);
    return () => clearTimeout(t);
  }, [isStreaming]);
  useEffect(() => {
    if (!followBottom) return;
    let raf = 0;
    const loop = () => {
      notifyGrowth();
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [followBottom, notifyGrowth]);

  // Auto-resize textarea up to ~50vh; beyond that the textarea's own
  // overflow-y-auto kicks in so the rest of the composer stays put.
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    const maxHeight = window.innerHeight * TEXTAREA_MAX_VH_FRACTION;
    // Floor the resting height ~30% taller than a single row (see TEXTAREA_MIN_PX)
    // so the box visibly starts larger; auto-grow up to the 50vh cap continues
    // to follow scrollHeight above that floor.
    ta.style.height = `${Math.min(
      Math.max(ta.scrollHeight, TEXTAREA_MIN_PX),
      maxHeight,
    )}px`;
  }, [inputValue]);

  // a11y (tr-h8l5): keyboard support for the composer "+" menu. On open, move
  // focus to the first enabled item; trap Tab within the menu; arrow-navigate
  // between items; Escape closes it and returns focus to the trigger.
  useEffect(() => {
    if (!plusMenuOpen) return;
    const menu = plusMenuRef.current;
    if (!menu) return;
    const items = () =>
      [...menu.querySelectorAll<HTMLButtonElement>('[role^="menuitem"]')].filter(
        (b) => !b.disabled,
      );
    items()[0]?.focus();
    const onKey = (e: KeyboardEvent) => {
      const focusable = items();
      if (e.key === "Escape") {
        e.preventDefault();
        setPlusMenuOpen(false);
        plusButtonRef.current?.focus();
      } else if (e.key === "Tab" && focusable.length) {
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      } else if (
        (e.key === "ArrowDown" || e.key === "ArrowUp") &&
        focusable.length
      ) {
        e.preventDefault();
        const idx = focusable.indexOf(document.activeElement as HTMLButtonElement);
        const delta = e.key === "ArrowDown" ? 1 : -1;
        focusable[(idx + delta + focusable.length) % focusable.length].focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [plusMenuOpen]);

  // Revoke any un-sent object URLs (and clear the error timer) on unmount.
  // Sent images hand their previewUrl to the message bubble, so those are
  // intentionally left alive for the session.
  useEffect(() => {
    return () => {
      for (const img of imagesRef.current) URL.revokeObjectURL(img.previewUrl);
      if (errorTimerRef.current) clearTimeout(errorTimerRef.current);
      if (capabilityWarningTimerRef.current) {
        clearTimeout(capabilityWarningTimerRef.current);
      }
      if (analyticsCloseTimerRef.current) {
        clearTimeout(analyticsCloseTimerRef.current);
      }
    };
  }, []);

  // Empty-state greeting: deterministic per clock hour. A seeded PRNG keyed on the
  // current hour means every reload within the same hour picks the SAME greeting,
  // and it changes only when the hour rolls over. The current-time Date and the
  // engine's RNG run only inside effects — never during render — so SSR and the
  // first client render both emit "" (no hydration mismatch).
  //
  // INITIAL pick runs in a layout effect so it commits before the browser paints:
  // the first visible frame already shows the greeting (no empty-frame flicker).
  useIsomorphicLayoutEffect(() => {
    const now = new Date();
    setGreeting(pickGreeting({ now, name: userName, random: mulberry32(hourSeed(now)) }));
  }, [userName]);

  // Hourly rotation: re-pick a fresh per-hour greeting exactly at the next
  // top-of-hour, then every hour after. The initial pick is handled by the layout
  // effect above, so this effect only schedules the timers (no eager compute).
  useEffect(() => {
    const compute = () => {
      const now = new Date();
      setGreeting(pickGreeting({ now, name: userName, random: mulberry32(hourSeed(now)) }));
    };
    let intervalId: ReturnType<typeof setInterval> | undefined;
    const now = new Date();
    const msToNextHour =
      (60 - now.getMinutes()) * 60_000 - now.getSeconds() * 1000 - now.getMilliseconds() + 50;
    const timeoutId = setTimeout(() => {
      compute();
      intervalId = setInterval(compute, 60 * 60 * 1000);
    }, msToNextHour);
    return () => {
      clearTimeout(timeoutId);
      if (intervalId) clearInterval(intervalId);
    };
  }, [userName]);

  // Fix 1: show the amber capability notice and auto-dismiss it after ~5s.
  const showCapabilityWarning = useCallback((msg: string) => {
    setCapabilityWarning(msg);
    if (capabilityWarningTimerRef.current) {
      clearTimeout(capabilityWarningTimerRef.current);
    }
    capabilityWarningTimerRef.current = setTimeout(
      () => setCapabilityWarning(null),
      5000,
    );
  }, []);

  // Fix 1: react to model-capability transitions. On losing image/file support
  // mid-conversation, auto-uncheck those context checkboxes (remembering which)
  // and surface a non-blocking warning; on regaining support, restore exactly
  // the ones we auto-unchecked.
  useEffect(() => {
    const prev = prevCapsRef.current;
    prevCapsRef.current = { vision: supportsVision, files: supportsFiles };
    // First resolve, or no capability change → nothing to do.
    if (
      prev === null ||
      (prev.vision === supportsVision && prev.files === supportsFiles)
    ) {
      return;
    }

    let lostImages = false;
    let lostFiles = false;

    // Images.
    if (prev.vision && !supportsVision) {
      const ids: string[] = [];
      for (const imgs of conversationImagesRef.current.values()) {
        for (const img of imgs) if (img.includedInContext) ids.push(img.id);
      }
      if (ids.length > 0) {
        ids.forEach((id) => onToggleImageContext(id));
        autoUncheckedImageIdsRef.current = ids;
        lostImages = true;
      }
    } else if (!prev.vision && supportsVision) {
      const remembered = new Set(autoUncheckedImageIdsRef.current);
      if (remembered.size > 0) {
        for (const imgs of conversationImagesRef.current.values()) {
          for (const img of imgs) {
            if (remembered.has(img.id) && !img.includedInContext) {
              onToggleImageContext(img.id);
            }
          }
        }
        autoUncheckedImageIdsRef.current = [];
      }
    }

    // Files (same pattern).
    if (prev.files && !supportsFiles) {
      const ids: string[] = [];
      for (const files of conversationFilesRef.current.values()) {
        for (const f of files) if (f.includedInContext) ids.push(f.id);
      }
      if (ids.length > 0) {
        ids.forEach((id) => onToggleFileContext(id));
        autoUncheckedFileIdsRef.current = ids;
        lostFiles = true;
      }
    } else if (!prev.files && supportsFiles) {
      const remembered = new Set(autoUncheckedFileIdsRef.current);
      if (remembered.size > 0) {
        for (const files of conversationFilesRef.current.values()) {
          for (const f of files) {
            if (remembered.has(f.id) && !f.includedInContext) {
              onToggleFileContext(f.id);
            }
          }
        }
        autoUncheckedFileIdsRef.current = [];
      }
    }

    if (lostImages && lostFiles) {
      showCapabilityWarning(
        "Выбранная модель не поддерживает изображения и файлы — они исключены из контекста",
      );
    } else if (lostImages) {
      showCapabilityWarning(
        "Выбранная модель не поддерживает изображения — они исключены из контекста",
      );
    } else if (lostFiles) {
      showCapabilityWarning(
        "Выбранная модель не поддерживает файлы — они исключены из контекста",
      );
    }
  }, [
    supportsVision,
    supportsFiles,
    onToggleImageContext,
    onToggleFileContext,
    showCapabilityWarning,
  ]);

  // Force web search off when switching to a model that can't web-search, so the
  // stale "on" never leaks into the request (web_search=true) or the result's
  // «Использован веб-поиск» meta — and restore it when a capable model returns.
  // Correcting the source state fixes the hint/toggle (which read it) as well.
  useEffect(() => {
    const prevSupported = prevWebSupportRef.current;
    prevWebSupportRef.current = supportsWebSearch;
    if (!supportsWebSearch) {
      // Model can't web-search → force the flag off (covers initial load too, not
      // just a live switch). Remember we did it, but only when it was actually on,
      // so a later return to a capable model restores the user's intent.
      if (webSearchEnabled) {
        onToggleWebSearch();
        autoDisabledWebRef.current = true;
      }
    } else if (prevSupported === false) {
      // Transition incapable → capable: restore only what WE auto-disabled (never
      // re-enable a search the user themselves turned off).
      if (autoDisabledWebRef.current && !webSearchEnabled) onToggleWebSearch();
      autoDisabledWebRef.current = false;
    }
  }, [supportsWebSearch, webSearchEnabled, onToggleWebSearch]);

  const showError = useCallback((msg: string) => {
    setImageError(msg);
    if (errorTimerRef.current) clearTimeout(errorTimerRef.current);
    errorTimerRef.current = setTimeout(() => setImageError(null), 3000);
  }, []);

  // Artifact canvas open/close — the same mount-then-animate / animate-then-
  // unmount dance as analytics. Declared before openAnalytics so that one can
  // close the canvas (the two panels are mutually exclusive in the right zone).
  const closeArtifactPanel = useCallback(() => {
    setArtifactOpen(false);
    if (artifactCloseTimerRef.current) clearTimeout(artifactCloseTimerRef.current);
    artifactCloseTimerRef.current = setTimeout(() => {
      setArtifactMounted(false);
      setOpenArtifact(null);
      artifactCloseTimerRef.current = null;
      // Return focus to the card that opened the canvas (a11y).
      lastFocusedRef.current?.focus();
    }, 200);
  }, []);

  // Analytics open/close with an animated exit. Opening mounts immediately and
  // cancels any pending unmount; closing plays the slide-out, then unmounts
  // after the wrapper's 200ms transition.
  const openAnalytics = useCallback(() => {
    closeArtifactPanel();
    if (analyticsCloseTimerRef.current) {
      clearTimeout(analyticsCloseTimerRef.current);
      analyticsCloseTimerRef.current = null;
    }
    // Mount in the hidden state first, then flip to visible on the next frame
    // so the wrapper transitions (enter animation plays instead of snapping in).
    setAnalyticsMounted(true);
    requestAnimationFrame(() => setAnalyticsOpen(true));
  }, [closeArtifactPanel]);
  const closeAnalytics = useCallback(() => {
    setAnalyticsOpen(false);
    if (analyticsCloseTimerRef.current) {
      clearTimeout(analyticsCloseTimerRef.current);
    }
    analyticsCloseTimerRef.current = setTimeout(() => {
      setAnalyticsMounted(false);
      analyticsCloseTimerRef.current = null;
    }, 200);
  }, []);
  const toggleAnalytics = useCallback(() => {
    if (analyticsOpen) closeAnalytics();
    else openAnalytics();
  }, [analyticsOpen, openAnalytics, closeAnalytics]);

  // Open a generated artifact in the canvas, closing analytics first.
  const openArtifactPanel = useCallback(
    (artifact: Artifact) => {
      lastFocusedRef.current =
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
      closeAnalytics();
      if (artifactCloseTimerRef.current) {
        clearTimeout(artifactCloseTimerRef.current);
        artifactCloseTimerRef.current = null;
      }
      setOpenArtifact(artifact);
      setArtifactMounted(true);
      requestAnimationFrame(() => setArtifactOpen(true));
    },
    [closeAnalytics],
  );

  // Switch the open canvas to another version of the same file (tr-qil). Refetch
  // that version's content from the artifact endpoint and swap it into the open
  // artifact, keeping version/version_count so the <n/m> switcher stays correct.
  // No-op if the open artifact lacks the keys (e.g. a heuristic/client artifact).
  const switchArtifactVersion = useCallback(
    async (version: number) => {
      const current = openArtifact;
      if (!current?.conversationId || !current.filename) return;
      try {
        const data = await fetchToolArtifact(
          current.conversationId,
          current.filename,
          version,
        );
        setOpenArtifact((prev) =>
          prev && prev.id === current.id
            ? {
                ...prev,
                content: data.content,
                version: data.version,
                versionCount: data.version_count,
              }
            : prev,
        );
      } catch {
        // Leave the current version shown on a failed fetch.
      }
    },
    [openArtifact],
  );

  // Remaining attachment slots (images + files share the MAX_ATTACHMENTS cap §3).
  const attachmentRoom = () =>
    MAX_ATTACHMENTS - imagesRef.current.length - filesRef.current.length;

  const addImages = useCallback(
    (files: File[]) => {
      if (!supportsVision || files.length === 0) return;

      let pendingError: string | null = null;
      const valid: File[] = [];
      for (const file of files) {
        if (!ACCEPTED_IMAGE_TYPES.includes(file.type)) {
          pendingError = "Поддерживаются только JPEG, PNG, WebP и GIF.";
          continue;
        }
        if (file.size > MAX_IMAGE_BYTES) {
          pendingError = "Размер изображения не должен превышать 5 МБ.";
          continue;
        }
        valid.push(file);
      }

      const room = attachmentRoom();
      if (room <= 0) {
        showError(`Не более ${MAX_ATTACHMENTS} вложений на сообщение.`);
        return;
      }
      const toAdd = valid.slice(0, room);
      if (valid.length > room) {
        pendingError = `Не более ${MAX_ATTACHMENTS} вложений на сообщение.`;
      }

      const created: AttachedImage[] = toAdd.map((file) => ({
        id: attachmentId(),
        file,
        base64: "",
        mediaType: file.type,
        previewUrl: URL.createObjectURL(file),
        loading: true,
      }));

      if (created.length > 0) {
        setAttachedImages((prev) => [...prev, ...created]);
        // Decode each file to base64 (pure payload, no data-URI prefix).
        for (const img of created) {
          const reader = new FileReader();
          reader.onload = () => {
            const result =
              typeof reader.result === "string" ? reader.result : "";
            const comma = result.indexOf(",");
            const base64 = comma >= 0 ? result.slice(comma + 1) : "";
            setAttachedImages((cur) =>
              cur.map((it) =>
                it.id === img.id ? { ...it, base64, loading: false } : it,
              ),
            );
          };
          reader.onerror = () => {
            URL.revokeObjectURL(img.previewUrl);
            setAttachedImages((cur) => cur.filter((it) => it.id !== img.id));
            showError("Не удалось прочитать изображение.");
          };
          reader.readAsDataURL(img.file);
        }
      }

      if (pendingError) showError(pendingError);
    },
    [supportsVision, showError],
  );

  const addFiles = useCallback(
    (files: File[]) => {
      if (!supportsFiles || files.length === 0) return;

      let pendingError: string | null = null;
      const valid: { file: File; mime: string; kind: AttachedFile["kind"] }[] = [];
      for (const file of files) {
        const type = classifyFile(file);
        if (!type) {
          pendingError = "Неподдерживаемый тип файла.";
          continue;
        }
        if (file.size > MAX_FILE_BYTES) {
          pendingError = "Размер файла не должен превышать 10 МБ.";
          continue;
        }
        valid.push({ file, mime: type.mime, kind: type.kind });
      }

      const room = attachmentRoom();
      if (room <= 0) {
        showError(`Не более ${MAX_ATTACHMENTS} вложений на сообщение.`);
        return;
      }
      const toAdd = valid.slice(0, room);
      if (valid.length > room) {
        pendingError = `Не более ${MAX_ATTACHMENTS} вложений на сообщение.`;
      }

      const created: AttachedFile[] = toAdd.map(({ file, mime, kind }) => ({
        id: attachmentId(),
        file,
        kind,
        filename: file.name,
        mediaType: mime,
        sizeBytes: file.size,
        tokenEstimate: 0,
        base64: "",
        loading: true,
      }));

      if (created.length > 0) {
        setAttachedFiles((prev) => [...prev, ...created]);
        for (const af of created) {
          const reader = new FileReader();
          reader.onload = () => {
            const buf =
              reader.result instanceof ArrayBuffer ? reader.result : null;
            if (!buf) {
              setAttachedFiles((cur) => cur.filter((it) => it.id !== af.id));
              showError("Не удалось прочитать файл.");
              return;
            }
            const bytes = new Uint8Array(buf);
            const base64 = bytesToBase64(bytes);
            // Token estimate per format (§3): text ~chars/4, PDF ~500/page,
            // DOCX ~by size (no client-side extraction without a new dep).
            const tokenEstimate =
              af.kind === "text"
                ? estimateTextTokens(new TextDecoder().decode(bytes))
                : af.kind === "pdf"
                  ? estimatePdfTokens(bytes)
                  : estimateDocxTokens(bytes.length);
            setAttachedFiles((cur) =>
              cur.map((it) =>
                it.id === af.id
                  ? { ...it, base64, tokenEstimate, loading: false }
                  : it,
              ),
            );
          };
          reader.onerror = () => {
            setAttachedFiles((cur) => cur.filter((it) => it.id !== af.id));
            showError("Не удалось прочитать файл.");
          };
          reader.readAsArrayBuffer(af.file);
        }
      }

      if (pendingError) showError(pendingError);
    },
    [supportsFiles, showError],
  );

  // Unified entry point for picker / drop / paste: route each file to the image
  // or file path by type, honoring the current model's capabilities (§5).
  const addAttachments = useCallback(
    (incoming: File[]) => {
      if (incoming.length === 0) return;
      const images: File[] = [];
      const docs: File[] = [];
      let skipped = false;
      for (const f of incoming) {
        if (f.type.startsWith("image/")) {
          if (supportsVision) images.push(f);
          else skipped = true;
        } else if (isSupportedFile(f)) {
          if (supportsFiles) docs.push(f);
          else skipped = true;
        } else {
          skipped = true;
        }
      }
      if (images.length > 0) addImages(images);
      if (docs.length > 0) addFiles(docs);
      // Only surface an error when nothing at all could be added (otherwise the
      // per-path handlers already reported any limit issues).
      if (skipped && images.length === 0 && docs.length === 0) {
        showError("Неподдерживаемый тип файла.");
      }
    },
    [supportsVision, supportsFiles, addImages, addFiles, showError],
  );

  const removeImage = useCallback((id: string) => {
    const target = imagesRef.current.find((i) => i.id === id);
    if (target) URL.revokeObjectURL(target.previewUrl);
    setAttachedImages((prev) => prev.filter((i) => i.id !== id));
  }, []);

  const removeFile = useCallback((id: string) => {
    setAttachedFiles((prev) => prev.filter((f) => f.id !== id));
  }, []);

  // Paste images from the clipboard anywhere in the window (§3).
  useEffect(() => {
    if (!supportsVision) return;
    const onPaste = (e: ClipboardEvent) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      const files: File[] = [];
      for (const item of Array.from(items)) {
        if (item.kind === "file" && item.type.startsWith("image/")) {
          const f = item.getAsFile();
          if (f) files.push(f);
        }
      }
      if (files.length > 0) {
        e.preventDefault();
        addImages(files);
      }
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [supportsVision, addImages]);

  const anyLoading =
    attachedImages.some((i) => i.loading) || attachedFiles.some((f) => f.loading);
  const hasContent =
    inputValue.trim().length > 0 ||
    attachedImages.length > 0 ||
    attachedFiles.length > 0;
  // Client-side mirror of the backend length cap: block the send before it
  // reaches the server (which would 422). Counts the raw length, matching the
  // backend Pydantic check (it validates `len(text)` against _MAX_TEXT_LEN).
  const overCharLimit = inputValue.length > MAX_MESSAGE_CHARS;
  const canSend =
    !isStreaming &&
    !isLoadingHistory &&
    !anyLoading &&
    !overCharLimit &&
    hasContent;

  // Only attachments the current model supports are sent; the rest stay visible
  // in the composer but are excluded from the payload (§4).
  const sendableImages = supportsVision ? attachedImages : [];
  const sendableFiles = supportsFiles ? attachedFiles : [];

  const submit = useCallback(() => {
    if (!canSend) return;
    // tr-n5b (MONEY): bail out synchronously if a send from this same tick is
    // already in flight — the React-state `canSend`/disabled guard updates only
    // after the tick, so without this a second same-tick submit would fire a
    // second billed POST.
    if (submittingRef.current) return;

    // Pre-send compatibility guard. History attachments toggled back into
    // context (their per-message checkbox re-checked) after the model-switch
    // warning would be re-injected by sendMessage and hard-fail at OpenRouter
    // with an incompatible-content error. Catch it here, synchronously, before
    // any request leaves the client: uncheck the offending attachments, warn,
    // and abort the send. The user's composed text is left untouched so a retry
    // (now without the incompatible content) — or a model switch — just works.
    const blockedImageIds: string[] = [];
    if (!supportsVision) {
      for (const imgs of conversationImagesRef.current.values()) {
        for (const img of imgs) {
          if (img.includedInContext) blockedImageIds.push(img.id);
        }
      }
    }
    const blockedFileIds: string[] = [];
    if (!supportsFiles) {
      for (const files of conversationFilesRef.current.values()) {
        for (const f of files) {
          if (f.includedInContext) blockedFileIds.push(f.id);
        }
      }
    }
    if (blockedImageIds.length > 0 || blockedFileIds.length > 0) {
      blockedImageIds.forEach((id) => onToggleImageContext(id));
      blockedFileIds.forEach((id) => onToggleFileContext(id));
      const lostImages = blockedImageIds.length > 0;
      const lostFiles = blockedFileIds.length > 0;
      showCapabilityWarning(
        lostImages && lostFiles
          ? "Выбранная модель не поддерживает изображения и файлы — они исключены из контекста"
          : lostImages
            ? "Выбранная модель не поддерживает изображения — они исключены из контекста"
            : "Выбранная модель не поддерживает файлы — они исключены из контекста",
      );
      return;
    }

    // Nothing the current model can accept and no text — don't send an empty turn.
    if (
      inputValue.trim().length === 0 &&
      sendableImages.length === 0 &&
      sendableFiles.length === 0
    ) {
      return;
    }
    // Engage the lock synchronously, immediately before the (fire-and-forget)
    // send — every early return above is a NON-sending path, so the lock is only
    // ever set when a real POST is about to leave the client. A 2nd submit in
    // this same tick now hits the `submittingRef.current` guard at the top and
    // returns before sending. Released on the next settled render (effect below).
    submittingRef.current = true;
    onSend(sendableImages, sendableFiles);
    // Clear the input attachment state WITHOUT revoking image previewUrls — the
    // sent ones are now owned by the message bubble in history.
    setAttachedImages([]);
    setAttachedFiles([]);
  }, [
    canSend,
    inputValue,
    sendableImages,
    sendableFiles,
    onSend,
    supportsVision,
    supportsFiles,
    onToggleImageContext,
    onToggleFileContext,
    showCapabilityWarning,
  ]);

  // tr-n5b (MONEY): release the same-tick submit lock once the send has settled.
  // After `submit` fires the (fire-and-forget) `onSend`, the parent synchronously
  // clears the composed text and then drives `isStreaming` true→false (or, on
  // failure, surfaces an error without ever streaming). Any of these is a fresh
  // render with a changed `inputValue`/`isStreaming`/`error`, i.e. a tick AFTER
  // the double-submit window, so clearing the ref here lets the next genuine
  // message — including a retry after an error — send normally. `error` must be
  // a dep: a conversation-create failure keeps the composed text (tr-w4fy.2), so
  // neither inputValue nor isStreaming ever changes on that path — without the
  // error release every later send would be a silent no-op until reload.
  useEffect(() => {
    submittingRef.current = false;
  }, [isStreaming, inputValue, error]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const handleFileInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    if (files.length > 0) addAttachments(files);
    // Reset so picking the same file again re-fires onChange.
    e.target.value = "";
  };

  // Drag & drop over the whole window. A depth counter avoids flicker as the
  // dragged item moves across child elements (each fires enter/leave).
  const onDragEnter = (e: React.DragEvent) => {
    if (!canAttach) return;
    e.preventDefault();
    dragDepth.current += 1;
    setIsDragging(true);
  };
  const onDragOver = (e: React.DragEvent) => {
    if (!canAttach) return;
    e.preventDefault();
  };
  const onDragLeave = (e: React.DragEvent) => {
    if (!canAttach) return;
    dragDepth.current -= 1;
    if (dragDepth.current <= 0) {
      dragDepth.current = 0;
      setIsDragging(false);
    }
  };
  const onDrop = (e: React.DragEvent) => {
    if (!canAttach) return;
    e.preventDefault();
    dragDepth.current = 0;
    setIsDragging(false);
    const files = Array.from(e.dataTransfer?.files ?? []);
    if (files.length > 0) addAttachments(files);
  };

  const dragActive = isDragging && canAttach;
  const isEmpty = !isLoadingHistory && messages.length === 0;

  // Edit-prognosis preceding context (§0.8). For each rendered (active-path)
  // message, the cumulative input+output tokens of every message BEFORE it —
  // i.e. the context an edited version of this turn would be re-sent with. The
  // running sum is taken before adding the current message, so index 0 is 0.
  // Threaded into each MessageBubble so the inline edit box's live prognosis
  // forecasts edited text + this branch's preceding context (not always 0).
  // Memoized on `messages` (audit L1/L2): both reduces depend ONLY on the
  // transcript, but ran on every ChatWindow render — including each composer
  // keystroke. useMemo skips them when `messages` is unchanged.
  const precedingHistoryTokensByIndex = useMemo(() => {
    const out: number[] = [];
    let running = 0;
    for (const m of messages) {
      out.push(running);
      if (m.tokensUsed) running += m.tokensUsed.input + m.tokensUsed.output;
    }
    return out;
  }, [messages]);

  // Context-window token warning (§3). History token usage + the pending prompt
  // + sendable attachments; warn (non-blocking) when it exceeds the model window.
  const contextWindow = selectedModel?.context_window ?? 0;
  const historyTokens = useMemo(
    () =>
      messages.reduce(
        (sum, m) =>
          sum + (m.tokensUsed ? m.tokensUsed.input + m.tokensUsed.output : 0),
        0,
      ),
    [messages],
  );
  const pendingTokens =
    Math.ceil(inputValue.length / CHARS_PER_TOKEN) +
    sendableImages.length * IMAGE_TOKENS +
    sendableFiles.reduce((s, f) => s + f.tokenEstimate, 0);
  const estimatedTotalTokens = historyTokens + pendingTokens;
  const contextWarning =
    contextWindow > 0 && estimatedTotalTokens > contextWindow;

  // "+" menu attach label per Step 2 (only shown when canAttach).
  const attachLabel =
    supportsVision && supportsFiles
      ? "Прикрепить файл или изображение"
      : supportsVision
        ? "Прикрепить изображение"
        : "Прикрепить файл";

  const acceptAttr = buildAccept({ images: supportsVision, files: supportsFiles });

  // Composer content lifted into a variable so it can be rendered in either
  // position — centered on the welcome screen, or fixed at the bottom once the
  // conversation has messages. Same DOM tree, just different parent wrappers.
  const composerBlock = (
    <>
      {error ? (
            <div
              role="alert"
              className="mb-2 flex items-start justify-between gap-3 rounded-lg border border-red-800 bg-red-900/30 px-4 py-2 text-sm text-red-400"
            >
              {/* Height-bounded + scrollable so a long error can never cover the
                  chat; also break long unbroken strings so they can't widen it
                  (tr-5zt). */}
              <span className="max-h-32 overflow-y-auto break-words whitespace-pre-wrap">
                {error}
              </span>
              <button
                type="button"
                onClick={onClearError}
                className="shrink-0 text-xs uppercase text-red-400 transition-colors hover:text-red-300"
              >
                Закрыть
              </button>
            </div>
          ) : null}

          {/* Floating composer. */}
          <div
            className={`rounded-2xl border bg-[var(--trinity-input)] shadow-lg transition-colors ${
              dragActive
                ? "border-emerald-500 ring-2 ring-inset ring-emerald-600/30"
                : "border-neutral-700"
            }`}
          >
            {attachedImages.length > 0 || attachedFiles.length > 0 ? (
              <div className="flex flex-col gap-2 px-3 pt-3">
                <div className="flex flex-wrap gap-2">
                  {attachedImages.map((img) => (
                    <div key={img.id} className="group relative h-20 w-20">
                      {img.loading ? (
                        <div className="relative h-20 w-20 overflow-hidden rounded-lg bg-neutral-800">
                          <div className="absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-white/10 to-transparent" />
                        </div>
                      ) : (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img
                          src={img.previewUrl}
                          alt="Вложение"
                          className={`h-20 w-20 rounded-lg object-cover transition-opacity ${
                            supportsVision ? "" : "opacity-40"
                          }`}
                        />
                      )}
                      <button
                        type="button"
                        onClick={() => removeImage(img.id)}
                        aria-label="Удалить изображение"
                        className="absolute left-1 top-1 hidden h-5 w-5 items-center justify-center rounded-full bg-black/60 text-xs leading-none text-white group-hover:flex hover:bg-black/80"
                      >
                        ×
                      </button>
                    </div>
                  ))}
                  {attachedFiles.map((f) => (
                    <FileChip
                      key={f.id}
                      file={f}
                      unsupported={!supportsFiles}
                      onRemove={() => removeFile(f.id)}
                    />
                  ))}
                </div>
                {(!supportsVision && attachedImages.length > 0) ||
                (!supportsFiles && attachedFiles.length > 0) ? (
                  <span className="text-xs text-amber-400">
                    [Не поддерживается текущей моделью]
                  </span>
                ) : null}
              </div>
            ) : null}

            <textarea
              ref={textareaRef}
              value={inputValue}
              onChange={(e) => onInputChange(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Напишите сообщение..."
              disabled={isStreaming || isLoadingHistory}
              rows={1}
              className="block max-h-[50vh] min-h-[44px] w-full resize-none overflow-y-auto bg-transparent px-4 py-2.5 text-sm leading-6 text-neutral-100 placeholder:text-neutral-500 outline-none disabled:opacity-60"
            />

            <div className="flex items-center gap-2 px-2.5 pb-2.5 pt-0.5">
              <input
                ref={fileInputRef}
                type="file"
                accept={acceptAttr}
                multiple
                className="hidden"
                onChange={handleFileInputChange}
              />

              {/* "+" menu: web search + attach image. */}
              <div className="relative">
                <button
                  ref={plusButtonRef}
                  type="button"
                  onClick={() => setPlusMenuOpen((v) => !v)}
                  aria-haspopup="menu"
                  aria-expanded={plusMenuOpen}
                  title="Добавить"
                  aria-label="Добавить"
                  className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-neutral-300 transition-colors hover:bg-neutral-700/60 hover:text-neutral-100 active:scale-95"
                >
                  <PlusIcon />
                </button>
                {/* Web-search active indicator: an emerald globe badge on the "+"
                    (where the toggle lives), mirroring the thinking badge on the
                    model icon. Absolute → zero layout impact; the cost note moved
                    into its tooltip, replacing the old full-width hint line. */}
                {effectiveWebSearch ? (
                  <span
                    className="pointer-events-none absolute -right-1 -top-1 rounded-full bg-[var(--trinity-input)] p-px text-emerald-500 shadow-[0_0_5px_1px_rgba(16,185,129,0.55)]"
                    title="Веб-поиск включён — запрос может стоить дороже"
                    aria-hidden="true"
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                      <circle cx="12" cy="12" r="9" fill="var(--trinity-input)" stroke="currentColor" strokeWidth="2" />
                      <path
                        d="M3 12h18M12 3c2.6 2.4 3.9 5.6 3.9 9s-1.3 6.6-3.9 9M12 3c-2.6 2.4-3.9 5.6-3.9 9s1.3 6.6 3.9 9"
                        stroke="currentColor"
                        strokeWidth="1.6"
                      />
                    </svg>
                  </span>
                ) : null}
                {plusMenuOpen ? (
                  <>
                    <button
                      type="button"
                      aria-hidden="true"
                      tabIndex={-1}
                      onClick={() => {
                        setPlusMenuOpen(false);
                        plusButtonRef.current?.focus();
                      }}
                      className="fixed inset-0 z-30 cursor-default"
                    />
                    <div
                      ref={plusMenuRef}
                      role="menu"
                      className="absolute bottom-full left-0 z-40 mb-2 w-64 animate-scaleIn rounded-xl border border-neutral-700 bg-neutral-800 p-1.5 shadow-2xl"
                    >
                      {/* Web search — the WHOLE row toggles via the same Apple
                          switch as the model menu (kept open on toggle so the flip
                          is visible). Disabled when the model can't web-search. */}
                      <button
                        type="button"
                        role="menuitemcheckbox"
                        aria-checked={effectiveWebSearch}
                        disabled={!supportsWebSearch}
                        onClick={onToggleWebSearch}
                        className={`flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left text-sm transition-colors disabled:cursor-not-allowed disabled:hover:bg-transparent ${
                          supportsWebSearch
                            ? "text-neutral-200 hover:bg-neutral-700"
                            : "text-neutral-500"
                        }`}
                        title={
                          supportsWebSearch
                            ? "Веб-поиск"
                            : "Модель не поддерживает веб-поиск"
                        }
                      >
                        <span
                          className={`shrink-0 ${
                            effectiveWebSearch
                              ? "text-emerald-500"
                              : "text-neutral-400"
                          }`}
                        >
                          <GlobeIcon />
                        </span>
                        <span className="flex-1">Веб-поиск</span>
                        <ToggleVisual
                          checked={effectiveWebSearch}
                          disabled={!supportsWebSearch}
                        />
                      </button>
                      {/* Attach option — hidden entirely when the model supports
                          neither images nor files (§2). Label reflects support. */}
                      {canAttach ? (
                        <>
                          <div className="my-1 h-px bg-neutral-700/70" />
                          <button
                            type="button"
                            role="menuitem"
                            disabled={
                              isStreaming ||
                              isLoadingHistory ||
                              attachedImages.length + attachedFiles.length >=
                                MAX_ATTACHMENTS
                            }
                            onClick={() => {
                              setPlusMenuOpen(false);
                              fileInputRef.current?.click();
                            }}
                            className="flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left text-sm text-neutral-200 transition-colors hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
                            title={attachLabel}
                          >
                            <span className="shrink-0 text-neutral-400">
                              <PaperclipIcon />
                            </span>
                            <span className="flex-1">{attachLabel}</span>
                          </button>
                        </>
                      ) : null}
                    </div>
                  </>
                ) : null}
              </div>

              {/* Right group: model name (opens a context menu — change model /
                  thinking toggle / effort submenu), then analytics + send. The
                  model control sits to the LEFT of the action buttons and
                  truncates (min-w-0 + max-width) rather than pushing them off. */}
              <div className="ml-auto flex min-w-0 items-center gap-2">
                <div className="min-w-0 max-w-[200px] sm:max-w-[260px]">
                  <ModelMenu
                    selectedModel={selectedModel}
                    disabled={isStreaming || models.length === 0}
                    thinkingEnabled={thinkingEnabled}
                    thinkingEffort={thinkingEffort}
                    toolsEnabled={toolsEnabled}
                    onOpenModelSelector={() => setModelModalOpen(true)}
                    onToggleThinking={onToggleThinking}
                    onSetThinkingEffort={onSetThinkingEffort}
                    onToggleTools={onToggleTools}
                    modelSelectorOpen={modelModalOpen}
                  />
                </div>

                {/* Analytics toggle (always present unless streaming-only). */}
                <button
                  type="button"
                  onClick={toggleAnalytics}
                  aria-pressed={analyticsOpen}
                  title="Аналитика"
                  aria-label="Аналитика"
                  className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border transition-colors active:scale-95 ${
                    analyticsOpen
                      ? "border-emerald-600 bg-emerald-600/20 text-emerald-400"
                      : "border-neutral-700 text-neutral-300 hover:bg-neutral-700/50 hover:text-neutral-100"
                  }`}
                >
                  <ChartIcon />
                </button>

                {isStreaming ? (
                  <button
                    type="button"
                    onClick={onStop}
                    title="Остановить"
                    aria-label="Остановить генерацию"
                    className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-neutral-700 text-neutral-100 transition-colors hover:bg-neutral-700/60"
                  >
                    <span className="block h-3 w-3 rounded-[2px] bg-current" />
                  </button>
                ) : hasContent ? (
                  <button
                    type="button"
                    onClick={submit}
                    disabled={!canSend}
                    title="Отправить"
                    aria-label="Отправить"
                    className="flex h-9 w-9 shrink-0 animate-scaleIn items-center justify-center rounded-lg bg-emerald-600 text-white transition-colors hover:bg-emerald-500 active:scale-95 disabled:cursor-not-allowed disabled:bg-emerald-600/40"
                  >
                    <SendIcon />
                  </button>
                ) : null}
              </div>
            </div>
          </div>

          {/* Transient hints (capability/context/length warnings + the >1000-char
              counter). No reserved min-height: the row collapses to nothing when
              empty so the footer caption sits right under the composer (tr-cy0.6.14
              — the field must hug the bottom). The old reservation existed for the
              web-search hint, which has since moved into the "+" tooltip, so the
              only things that ever fill this row are rare/typing-time notices; a
              brief shift when one appears is acceptable. */}
          <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 px-1 text-xs text-neutral-600">
              {capabilityWarning ? (
                <span
                  className="flex items-center gap-1.5 text-amber-400"
                  role="status"
                >
                  <span>⚠ {capabilityWarning}</span>
                  <button
                    type="button"
                    onClick={() => setCapabilityWarning(null)}
                    aria-label="Скрыть предупреждение"
                    className="text-amber-400/70 transition-colors hover:text-amber-300"
                  >
                    ✕
                  </button>
                </span>
              ) : null}
              {imageError ? (
                <span className="text-red-400" role="alert">
                  {imageError}
                </span>
              ) : null}
              {contextWarning ? (
                <span className="text-amber-400" role="alert">
                  ⚠ Вложения (~{estimatedTotalTokens.toLocaleString("ru-RU")}{" "}
                  токенов) превышают контекст модели (
                  {contextWindow.toLocaleString("ru-RU")}). Запрос может быть
                  обрезан.
                </span>
              ) : null}
              {overCharLimit ? (
                <span className="text-red-400" role="alert">
                  Сообщение слишком длинное: максимум{" "}
                  {MAX_MESSAGE_CHARS.toLocaleString("ru-RU")} символов
                </span>
              ) : null}
              {inputValue.length > 1000 ? (
                // Near the cap (>=90%) switch to the «N / лимит» form and tint it
                // so the user sees how close they are; well under it, the plain
                // legacy counter is preserved unchanged.
                <span
                  className={`ml-auto tabular-nums ${
                    overCharLimit
                      ? "text-red-400"
                      : inputValue.length >= MAX_MESSAGE_CHARS * 0.9
                        ? "text-amber-400"
                        : "text-neutral-500"
                  }`}
                >
                  {inputValue.length >= MAX_MESSAGE_CHARS * 0.9
                    ? `${inputValue.length.toLocaleString(
                        "ru-RU",
                      )} / ${MAX_MESSAGE_CHARS.toLocaleString("ru-RU")}`
                    : `${inputValue.length} символов`}
                </span>
              ) : null}
            </div>

          <p className="mt-1.5 text-center text-xs text-neutral-600">
            Сгенерировано ИИ — только для справки.
          </p>
    </>
  );

  // Factored out so the wide (static column) and narrow (drawer overlay) layouts
  // can share one instance without duplicating props (tr-qo2r).
  const analyticsPanel = (
    <MicroAnalytics
      messages={messages}
      conversationImages={conversationImages}
      conversationFiles={conversationFiles}
      inputValue={inputValue}
      pendingImages={attachedImages}
      pendingFiles={attachedFiles}
      selectedModel={selectedModel}
      branchTotals={branchTotals}
      onClose={closeAnalytics}
    />
  );

  return (
    <div className="flex h-full min-h-0 w-full">
      <section
        role="main"
        aria-label="Чат"
        onDragEnter={onDragEnter}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        className="relative flex min-w-0 flex-1 flex-col bg-[var(--trinity-canvas)]"
      >
        {/* Centered conversation title with a pin/rename/delete dropdown. */}
        {conversationMenu ? (
          <div
            className={`flex items-center justify-center px-4 py-3 ${
              sidebarOpen ? "" : "pl-14"
            }`}
          >
            <TitleHeader
              title={conversationTitle || "Новый диалог"}
              menu={conversationMenu}
            />
          </div>
        ) : (
          <div className="h-3" />
        )}

        {isLoadingHistory ? (
          // Perceptible-load skeleton (>200ms): top-aligned shimmer mimicking the
          // feed. Nothing for the first 200ms so a fast/cached open never flashes.
          <div className="flex-1 overflow-y-auto [scrollbar-gutter:stable_both-edges]">
            {showSkeleton ? <ChatFeedSkeleton /> : null}
          </div>
        ) : isEmpty ? (
          /* Welcome state — centered greeting + composer, no message list. */
          <div className="flex flex-1 animate-fadeIn flex-col items-center justify-center gap-10 px-4 pb-10">
            <div className="flex items-center gap-5">
              <NeuralIcon size={80} />
              <h1 className="flex items-center gap-3 text-4xl font-light tracking-tight text-neutral-100 md:text-5xl lg:text-6xl">
                <span className="text-[#c3c2b8]">{greeting}</span>
              </h1>
            </div>
            <div className="w-full max-w-[816px]">{composerBlock}</div>
          </div>
        ) : (
          /* Normal state — scrolling message list with composer pinned to
             the bottom of the chat column. */
          <div className="relative flex min-h-0 flex-1 flex-col">
            {/* a11y (tr-58qa, WCAG 1.3.1): a heading names the transcript for
                screen-reader navigation; visually hidden (the title bar shows it). */}
            <h1 className="sr-only">
              Диалог «{conversationTitle || "Новый диалог"}»
            </h1>
            <div ref={listRef} className="flex-1 overflow-y-auto [scrollbar-gutter:stable_both-edges]">
              {/* a11y (tr-58qa, WCAG 4.1.3): role=log + aria-live=polite makes the
                  transcript a live region so new messages — and the streaming
                  answer as it grows — are announced to screen readers. */}
              <div
                role="log"
                aria-live="polite"
                aria-label="Сообщения диалога"
                className="mx-auto flex w-full max-w-[768px] animate-fadeIn flex-col gap-6 px-4 py-6"
              >
                {messages.map((m, i) => (
                    <MessageBubble
                      key={m.id}
                      message={m}
                      isLast={i === messages.length - 1}
                      images={conversationImages.get(m.id)}
                      files={conversationFiles.get(m.id)}
                      precedingHistoryTokens={precedingHistoryTokensByIndex[i]}
                      onToggleImageContext={onToggleImageContext}
                      onToggleFileContext={onToggleFileContext}
                      conversationVersions={conversationVersions}
                      onLoadVersions={onLoadVersions}
                      onSwitchVersion={onSwitchVersion}
                      onRegenerate={onRegenerate}
                      onEditAndResend={onEditAndResend}
                      onResend={onResend}
                      selectedModel={selectedModel}
                      onOpenArtifact={openArtifactPanel}
                      conversationId={conversationId}
                    />
                  ))}
                </div>
            </div>
            <div className="relative mx-auto w-full max-w-[816px] px-4 pb-2">
              {/* Anchored to the composer's top edge (bottom-full) so it always
                  floats in the gap ABOVE the composer, regardless of the
                  composer's (variable) height — a fixed bottom-N offset on the
                  whole column lands inside a short composer (tr-3lrl). */}
              {/* Kept mounted (not conditionally rendered) so its appearance and
                  disappearance fade+scale in/out; hidden state is non-interactive
                  and out of the tab order (tr-9dsz). Visibility is the distance
                  threshold from useStickToBottom, not the auto-follow flag. */}
              <button
                type="button"
                onClick={scrollToBottom}
                aria-label="К последнему сообщению"
                aria-hidden={!showScrollButton}
                tabIndex={showScrollButton ? 0 : -1}
                className={`absolute bottom-full left-1/2 z-10 mb-3 -translate-x-1/2 rounded-full border border-neutral-700 bg-neutral-800/90 p-2 text-neutral-200 shadow-lg backdrop-blur transition-all duration-200 ease-out hover:bg-neutral-700 ${
                  showScrollButton
                    ? "translate-y-0 scale-100 opacity-100"
                    : "pointer-events-none translate-y-1 scale-90 opacity-0"
                }`}
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path d="M12 5v14M6 13l6 6 6-6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
              {composerBlock}
            </div>
          </div>
        )}

        {/* Drag-and-drop overlay. */}
        {dragActive ? (
          <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center bg-neutral-950/70">
            <div className="rounded-2xl border-2 border-dashed border-emerald-500 px-8 py-6 text-sm font-medium text-emerald-400">
              {supportsVision && supportsFiles
                ? "Перетащите файлы или изображения сюда"
                : supportsVision
                  ? "Перетащите изображения сюда"
                  : "Перетащите файлы сюда"}
            </div>
          </div>
        ) : null}

        {/* Model selector modal — trigger lives in the composer. */}
        <ModelSelector
          models={models}
          selectedModelId={selectedModelId}
          disabled={isStreaming}
          onChange={onModelChange}
          hideTrigger
          open={modelModalOpen}
          onOpenChange={setModelModalOpen}
        />
      </section>

      {analyticsMounted ? (
        isWideCanvas ? (
          // Desktop (≥1024px): static right column. The animated wrapper owns the
          // enter/exit — it slides in from / out to the right and fades.
          <div
            className={`h-full transition-all duration-200 ease-out ${
              analyticsOpen
                ? "translate-x-0 opacity-100"
                : "pointer-events-none translate-x-4 opacity-0"
            }`}
          >
            {analyticsPanel}
          </div>
        ) : (
          // Narrow (<1024px): right-anchored drawer overlay + backdrop. As a
          // static column next to the sidebar it squeezed the chat into an
          // unusable ~115px sliver (768px) and squeezed out the model selector;
          // overlaying keeps the chat full-width behind it (tr-qo2r).
          <>
            <div
              className={`fixed inset-0 z-40 bg-black/60 transition-opacity duration-200 ease-out ${
                analyticsOpen ? "opacity-100" : "pointer-events-none opacity-0"
              }`}
              onClick={closeAnalytics}
              aria-hidden="true"
            />
            <div
              className={`fixed inset-y-0 right-0 z-50 transition-transform duration-200 ease-out ${
                analyticsOpen ? "translate-x-0" : "translate-x-full"
              }`}
            >
              {analyticsPanel}
            </div>
          </>
        )
      ) : null}

      {artifactMounted && openArtifact ? (
        isWideCanvas ? (
          // Desktop: right-anchored split column (mirrors analytics). The chat
          // section is flex-1 min-w-0, so it narrows to make room for the canvas.
          <div
            className={`h-full min-w-[420px] transition-all duration-200 ease-out lg:w-[46%] xl:w-[50%] ${
              artifactOpen
                ? "translate-x-0 opacity-100"
                : "pointer-events-none translate-x-4 opacity-0"
            }`}
          >
            <ArtifactPanel
              artifact={openArtifact}
              view={artifactView}
              onChangeView={setArtifactView}
              onClose={closeArtifactPanel}
              onSwitchVersion={switchArtifactVersion}
            />
          </div>
        ) : (
          // Narrow: fullscreen modal overlay (focus-trapped, body scroll locked).
          <div
            className={`fixed inset-0 z-50 transition-opacity duration-200 ease-out ${
              artifactOpen ? "opacity-100" : "pointer-events-none opacity-0"
            }`}
          >
            <ArtifactPanel
              artifact={openArtifact}
              view={artifactView}
              onChangeView={setArtifactView}
              onClose={closeArtifactPanel}
              onSwitchVersion={switchArtifactVersion}
              fullscreen
            />
          </div>
        )
      ) : null}
    </div>
  );
}

// A composer preview chip for an attached non-image file. Shows the filename,
// kind, size and token estimate; dimmed with an amber border when the current
// model can't accept it (§4).
function FileChip({
  file,
  unsupported,
  onRemove,
}: {
  file: AttachedFile;
  unsupported: boolean;
  onRemove: () => void;
}) {
  return (
    <div
      className={`group relative flex h-20 w-44 items-center gap-2.5 overflow-hidden rounded-lg border bg-neutral-800 px-3 ${
        unsupported ? "border-amber-500/50" : "border-neutral-700"
      }`}
    >
      <span
        className={`shrink-0 ${unsupported ? "text-amber-400" : "text-neutral-400"}`}
      >
        <FileIcon />
      </span>
      <div className="min-w-0 flex-1">
        <div
          className="truncate text-xs font-medium text-neutral-200"
          title={file.filename}
        >
          {file.filename}
        </div>
        <div className="mt-0.5 text-[10px] uppercase tracking-wide text-neutral-500">
          {file.kind}
        </div>
        <div className="mt-0.5 font-mono text-[10px] text-neutral-500">
          {file.loading
            ? "…"
            : `${formatFileSize(file.sizeBytes)} · ~${file.tokenEstimate.toLocaleString("ru-RU")} ток.`}
        </div>
      </div>
      <button
        type="button"
        onClick={onRemove}
        aria-label="Удалить файл"
        className="absolute right-1 top-1 hidden h-5 w-5 items-center justify-center rounded-full bg-black/60 text-xs leading-none text-white group-hover:flex hover:bg-black/80"
      >
        ×
      </button>
      {file.loading ? (
        <div className="pointer-events-none absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-white/5 to-transparent" />
      ) : null}
    </div>
  );
}

function TitleHeader({
  title,
  menu,
}: {
  title: string;
  menu: ConversationMenuActions;
}) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(title);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  if (editing) {
    return (
      <input
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          if (draft.trim().length > 0 && draft.trim() !== title) {
            menu.onRename(draft.trim());
          }
          setEditing(false);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") e.currentTarget.blur();
          else if (e.key === "Escape") {
            setDraft(title);
            setEditing(false);
          }
        }}
        className="w-64 rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-1.5 text-center text-sm text-neutral-100 outline-none focus:border-emerald-600"
      />
    );
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        className="flex max-w-[60vw] items-center gap-1.5 rounded-lg px-2 py-1 text-sm font-medium text-neutral-200 transition-colors hover:bg-neutral-800"
      >
        <span className="truncate">{title}</span>
        <svg
          className="h-4 w-4 shrink-0 text-neutral-500"
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
          aria-hidden="true"
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4 4-4" />
        </svg>
      </button>
      {open ? (
        <>
          <button
            type="button"
            aria-hidden="true"
            tabIndex={-1}
            onClick={() => setOpen(false)}
            className="fixed inset-0 z-30 cursor-default"
          />
          <div
            role="menu"
            className="absolute left-1/2 top-full z-40 mt-1 w-44 -translate-x-1/2 animate-scaleIn overflow-hidden rounded-lg border border-neutral-700 bg-neutral-800 py-1 shadow-xl"
          >
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                menu.onTogglePin();
              }}
              className="flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-sm text-neutral-200 transition-colors hover:bg-neutral-700"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path
                  d="M9 4h6l-1 6 3 3v2H7v-2l3-3-1-6zM12 15v5"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              {menu.isPinned ? "Открепить" : "Закрепить"}
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                setDraft(title);
                setEditing(true);
              }}
              className="flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-sm text-neutral-200 transition-colors hover:bg-neutral-700"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path
                  d="M4 20h4l10-10-4-4L4 16v4zM14 6l4 4"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              Переименовать
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                menu.onDelete();
              }}
              className="flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-sm text-red-400 transition-colors hover:bg-red-900/30"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path
                  d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              Удалить
            </button>
          </div>
        </>
      ) : null}
    </div>
  );
}

export const ChatWindow = memo(ChatWindowInner);
ChatWindow.displayName = "ChatWindow";

export default ChatWindow;
