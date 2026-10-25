"use client";

import type { Artifact } from "@/lib/artifacts";
import { artifactLabel } from "@/lib/artifacts";

// Document glyph: a sheet with a folded corner and a few text lines.
function DocGlyph() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path d="M14 3v5h5" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <path
        d="M8.5 13h7M8.5 16.5h7M8.5 9.5h2"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

// Code glyph: angle brackets.
function CodeGlyph() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M9 8l-4 4 4 4M15 8l4 4-4 4"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function DownloadGlyph() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M12 3v12m0 0l-4-4m4 4l4-4M5 21h14"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// Compact, clickable card shown in the chat stream for a generated document.
// Clicking the body opens the canvas; the separate download button (a sibling,
// never nested, so the markup stays valid and the actions don't overlap) saves
// the file without opening the panel.
export function ArtifactCard({
  artifact,
  onOpen,
  onDownload,
  generating,
}: {
  artifact: Artifact;
  onOpen: () => void;
  onDownload: () => void;
  // While the answer is still streaming the document is forming: the icon
  // pulses, the label reads «Генерация…» and download is disabled.
  generating?: boolean;
}) {
  return (
    <div className="relative my-1.5 w-fit max-w-[768px] animate-slideUp">
      <button
        type="button"
        onClick={onOpen}
        aria-label={`Открыть документ: ${artifact.title}`}
        className="flex w-full items-center gap-3 rounded-xl border border-neutral-700/60 bg-neutral-800/50 py-2.5 pl-2.5 pr-28 text-left transition-colors hover:border-neutral-600 hover:bg-neutral-800 active:scale-[0.99] focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60"
      >
        <span
          className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-neutral-700/60 text-neutral-300 ${
            generating ? "animate-pulse" : ""
          }`}
        >
          {artifact.kind === "markdown" ? <DocGlyph /> : <CodeGlyph />}
        </span>
        <span className="min-w-0">
          <span className="block truncate text-sm font-medium text-neutral-100">
            {artifact.title}
          </span>
          <span className="block truncate text-xs text-neutral-500">
            {generating ? "Генерация…" : artifactLabel(artifact)}
          </span>
        </span>
      </button>
      <button
        type="button"
        disabled={generating}
        onClick={(e) => {
          e.stopPropagation();
          onDownload();
        }}
        aria-label={`Скачать ${artifact.title}`}
        title="Скачать"
        className="absolute right-2 top-1/2 flex -translate-y-1/2 items-center gap-1.5 rounded-lg border border-neutral-700/60 px-2.5 py-1.5 text-xs text-neutral-300 transition-colors hover:border-neutral-600 hover:bg-neutral-700/60 hover:text-neutral-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-neutral-700/60 disabled:hover:bg-transparent disabled:hover:text-neutral-300"
      >
        <DownloadGlyph />
        <span className="hidden sm:inline">Скачать</span>
      </button>
    </div>
  );
}
