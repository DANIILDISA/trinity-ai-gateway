"use client";

import { useEffect, useRef, useState } from "react";

import type { Artifact } from "@/lib/artifacts";
import { Markdown } from "./Markdown";
import { CodeHighlight } from "./CodeHighlight";
import { VersionSwitcher } from "./VersionSwitcher";

export type ArtifactView = "preview" | "code";

function EyeIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z" stroke="currentColor" strokeWidth="1.6" />
      <circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}

function CodeIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M9 8l-4 4 4 4M15 8l4 4-4 4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function CopyIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="9" y="9" width="11" height="11" rx="2" stroke="currentColor" strokeWidth="1.6" />
      <path d="M5 15V5a2 2 0 0 1 2-2h10" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M5 13l4 4L19 7" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function ChevronDownIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M6 9l6 6 6-6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function RegenerateIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M20 11a8 8 0 1 0-2.3 5.7" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      <path d="M20 4v5h-5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function kindSuffix(artifact: Artifact): string {
  if (artifact.kind === "markdown") return "MD";
  return (artifact.language ?? "code").toUpperCase();
}

// Best-effort conversion of markdown to plain text for the "copy as plain text"
// option — strips the most common inline/heading syntax.
function toPlainText(markdown: string): string {
  return markdown
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/\*\*(.+?)\*\*/g, "$1")
    .replace(/([*_])(.+?)\1/g, "$2")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\[(.+?)\]\((.+?)\)/g, "$1");
}

const segClass = (active: boolean) =>
  `flex items-center justify-center rounded px-1.5 py-1 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60 ${
    active
      ? "bg-neutral-700 text-neutral-100"
      : "text-neutral-400 hover:text-neutral-200"
  }`;

// The artifact canvas: a header (segmented Preview/Code toggle + title, and a
// Copy split-button / regenerate / close group) over an independently scrolling
// body. Width and slide animation are owned by the surrounding layout.
export function ArtifactPanel({
  artifact,
  view,
  onChangeView,
  onClose,
  onRegenerate,
  onCopy,
  onSwitchVersion,
  fullscreen,
}: {
  artifact: Artifact;
  view: ArtifactView;
  onChangeView: (view: ArtifactView) => void;
  onClose: () => void;
  onRegenerate?: () => void;
  onCopy?: (format: "markdown" | "plain") => void;
  // Switch the canvas to another version of THIS file (tr-qil). Called with the
  // 1-based target version; the owner refetches and updates the open artifact.
  // The <n/m> switcher only renders when this is set and version_count > 1.
  onSwitchVersion?: (version: number) => void;
  // On narrow screens the panel renders as a fullscreen modal overlay: it traps
  // focus, locks body scroll and exposes role=dialog. In the desktop split it is
  // a labelled region and the page stays interactive.
  fullscreen?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const previewBtnRef = useRef<HTMLButtonElement>(null);
  const menuOpenRef = useRef(menuOpen);
  menuOpenRef.current = menuOpen;

  // Escape closes an open copy menu first, otherwise the panel.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (menuOpenRef.current) {
        setMenuOpen(false);
        return;
      }
      onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Move focus to the first header control when the panel opens. For code
  // artifacts the Preview toggle is hidden, so fall back to the first button.
  useEffect(() => {
    // a11y (tr-34yj): remember the opener (the artifact card) so focus returns
    // to it on close instead of dropping to <body> (WCAG 2.4.3).
    const opener = document.activeElement as HTMLElement | null;
    (
      previewBtnRef.current ??
      rootRef.current?.querySelector<HTMLButtonElement>("button")
    )?.focus();
    return () => opener?.focus();
  }, []);

  // Lock body scroll only while the fullscreen overlay is up. This panel is the
  // only inline setter of body overflow, so the baseline to restore is "" (the
  // base scroll behavior comes from CSS) — capturing/restoring a "prev" value is
  // fragile under React's dev double-invoke and can leave the lock stuck.
  useEffect(() => {
    if (!fullscreen) return;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = "";
    };
  }, [fullscreen]);

  // Focus trap — only in fullscreen, where the rest of the page is inert.
  const onRootKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (!fullscreen || e.key !== "Tab") return;
    const root = rootRef.current;
    if (!root) return;
    const focusable = root.querySelectorAll<HTMLElement>(
      'button, [href], input, [tabindex]:not([tabindex="-1"])',
    );
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };

  const copy = async (format: "markdown" | "plain") => {
    const text =
      format === "plain" ? toPlainText(artifact.content) : artifact.content;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Clipboard unavailable (insecure context) — ignore.
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
    setMenuOpen(false);
    onCopy?.(format);
  };

  const isServer = artifact.source === "server";
  const versionCount = artifact.versionCount ?? 1;
  const version = artifact.version ?? 1;
  // The interactive <n/m> switcher renders only for a multi-version server
  // artifact whose owner wired the refetch. When it's absent (e.g. download-only
  // contexts) but the file still has versions, fall back to a subtle "v{n}" hint.
  const showSwitcher = isServer && versionCount > 1 && !!onSwitchVersion;
  const showVersion = isServer && !showSwitcher && version > 1;
  const empty = artifact.content.trim().length === 0;
  // Code artifacts have no meaningful "preview": rendering source through Markdown
  // turns code into HTML (a Python `# comment` becomes an <h1>, the file is
  // unreadable). Only markdown previews — code always shows as code, and the
  // Preview/Code toggle is hidden for it (tr-893i).
  const canPreview = artifact.kind === "markdown";

  return (
    <div
      ref={rootRef}
      role={fullscreen ? "dialog" : "region"}
      aria-modal={fullscreen ? true : undefined}
      aria-label={`Документ ${artifact.title}`}
      onKeyDown={onRootKeyDown}
      className="flex h-full flex-col bg-[var(--trinity-canvas)]"
    >
      <header className="flex items-center justify-between gap-3 border-b border-neutral-800 px-4 py-3">
        <div className="flex min-w-0 items-center gap-3">
          {canPreview ? (
            <div className="flex items-center gap-0.5 rounded-md bg-neutral-800 p-0.5">
              <button
                ref={previewBtnRef}
                type="button"
                aria-label="Превью"
                aria-pressed={view === "preview"}
                onClick={() => onChangeView("preview")}
                className={segClass(view === "preview")}
              >
                <EyeIcon />
              </button>
              <button
                type="button"
                aria-label="Код"
                aria-pressed={view === "code"}
                onClick={() => onChangeView("code")}
                className={segClass(view === "code")}
              >
                <CodeIcon />
              </button>
            </div>
          ) : null}
          <h2 className="flex min-w-0 items-baseline gap-1.5 text-sm">
            <span className="truncate font-medium text-neutral-100">
              {artifact.title}
            </span>
            <span className="shrink-0 text-neutral-500">· {kindSuffix(artifact)}</span>
            {showVersion ? (
              <span className="shrink-0 text-xs text-neutral-500">v{version}</span>
            ) : null}
          </h2>
          {showSwitcher ? (
            <VersionSwitcher
              index={version - 1}
              count={versionCount}
              onPrev={() => onSwitchVersion?.(version - 1)}
              onNext={() => onSwitchVersion?.(version + 1)}
              className="shrink-0"
            />
          ) : null}
        </div>

        <div className="flex shrink-0 items-center gap-1.5">
          <div className="relative flex items-center">
            <button
              type="button"
              onClick={() => copy("markdown")}
              className="flex items-center gap-1.5 rounded-l-md border border-neutral-700/70 px-2.5 py-1.5 text-xs text-neutral-300 transition-colors hover:bg-neutral-800 hover:text-neutral-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60"
            >
              {copied ? <CheckIcon /> : <CopyIcon />}
              <span>{copied ? "Скопировано" : "Скопировать"}</span>
            </button>
            <button
              type="button"
              aria-label="Параметры копирования"
              onClick={() => setMenuOpen((o) => !o)}
              className="flex items-center rounded-r-md border border-l-0 border-neutral-700/70 px-1.5 py-1.5 text-neutral-300 transition-colors hover:bg-neutral-800 hover:text-neutral-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60"
            >
              <ChevronDownIcon />
            </button>
            {menuOpen ? (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setMenuOpen(false)} />
                <div className="absolute right-0 top-full z-20 mt-1 w-56 overflow-hidden rounded-lg border border-neutral-700 bg-neutral-800 py-1 shadow-lg">
                  <button
                    type="button"
                    onClick={() => copy("markdown")}
                    className="block w-full px-3 py-1.5 text-left text-xs text-neutral-300 hover:bg-neutral-700 hover:text-neutral-100"
                  >
                    Копировать как Markdown
                  </button>
                  <button
                    type="button"
                    onClick={() => copy("plain")}
                    className="block w-full px-3 py-1.5 text-left text-xs text-neutral-300 hover:bg-neutral-700 hover:text-neutral-100"
                  >
                    Копировать как обычный текст
                  </button>
                </div>
              </>
            ) : null}
          </div>

          {onRegenerate ? (
            <button
              type="button"
              aria-label="Перегенерировать"
              title="Перегенерировать"
              onClick={onRegenerate}
              className="flex items-center rounded-md p-1.5 text-neutral-400 transition-colors hover:bg-neutral-800 hover:text-neutral-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60"
            >
              <RegenerateIcon />
            </button>
          ) : null}

          <button
            type="button"
            aria-label="Закрыть"
            onClick={onClose}
            className="flex items-center rounded-md p-1.5 text-neutral-400 transition-colors hover:bg-neutral-800 hover:text-neutral-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60"
          >
            <CloseIcon />
          </button>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-auto px-4 py-4">
        {empty ? (
          <p className="py-12 text-center text-sm text-neutral-500">Документ пуст</p>
        ) : canPreview && view === "preview" ? (
          <Markdown content={artifact.content} />
        ) : (
          <div className="overflow-hidden rounded-lg bg-[#161615]">
            <div className="px-4 pt-2.5 font-mono text-[11px] tracking-wide text-neutral-500">
              {artifact.language ?? "markdown"}
            </div>
            <CodeHighlight
              code={artifact.content}
              language={artifact.language ?? "markdown"}
            />
          </div>
        )}
      </div>
    </div>
  );
}
