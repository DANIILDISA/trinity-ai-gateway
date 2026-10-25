"use client";

import { useState } from "react";

import type { Artifact } from "@/lib/artifacts";
import type { ToolArtifact } from "@/lib/toolArtifact";
import { toolArtifactToArtifact } from "@/lib/toolArtifact";
import {
  downloadToolArtifact,
  fetchToolArtifact,
} from "@/lib/downloadToolArtifact";

// Document glyph (sheet + folded corner + text lines) — matches the reference
// artifact card and the light-path ArtifactCard.
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

function SpinnerGlyph() {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      className="animate-spin"
    >
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.25" strokeWidth="2" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

// The card for a model-authored file, shown under the answer and above the
// action row (tr-cy0.2.13, reference screenshot_2). Clicking the card body opens
// the file in the canvas panel (tr-lqn) — its content is server-side, so the
// body click fetches the latest version and hands a canvas-ready Artifact to
// onOpenArtifact. The «Скачать» button is a separate sibling action (never
// nested, so the markup stays valid) that saves the file without opening the
// panel. Both actions need the conversation id to reach the artifact endpoint.
export function ToolArtifactCard({
  artifact,
  conversationId,
  onOpenArtifact,
}: {
  artifact: ToolArtifact;
  conversationId?: string;
  onOpenArtifact?: (artifact: Artifact) => void;
}) {
  // `download` drives the «Скачать» button; `open` drives the body-click fetch.
  // They're independent so an in-flight open doesn't disable download (or v.v.).
  const [download, setDownload] = useState<"idle" | "loading" | "error">("idle");
  const [open, setOpen] = useState<"idle" | "loading" | "error">("idle");
  // No conversation id (e.g. a brand-new chat before its id resolves) → nothing
  // to fetch from, so both actions are disabled rather than failing silently.
  const canFetch = !!conversationId;
  const canOpen = canFetch && !!onOpenArtifact && open !== "loading";
  const canDownload = canFetch && download !== "loading";

  const onDownload = async () => {
    if (!conversationId) return;
    setDownload("loading");
    try {
      await downloadToolArtifact(conversationId, artifact.filename);
      setDownload("idle");
    } catch {
      setDownload("error");
    }
  };

  const onOpen = async () => {
    if (!conversationId || !onOpenArtifact) return;
    setOpen("loading");
    try {
      const data = await fetchToolArtifact(conversationId, artifact.filename);
      onOpenArtifact(
        toolArtifactToArtifact(artifact, data.content, {
          version: data.version,
          versionCount: data.version_count,
          conversationId,
        }),
      );
      setOpen("idle");
    } catch {
      setOpen("error");
    }
  };

  // Surface the most recent failure in the subtitle (open or download).
  const errored = open === "error" || download === "error";
  const errorText =
    download === "error" ? "Не удалось скачать" : "Не удалось открыть";

  // No vertical margin: these cards render as direct flex children of the
  // message column (group/msg, `flex flex-col gap-1`), so the 4px flex gap
  // already separates stacked cards — a per-card my-* would be additive and
  // reopen the wide gap (tr-cy0.6.13: cards must sit almost flush).
  return (
    <div className="relative my-0 w-fit max-w-[768px] animate-slideUp">
      <button
        type="button"
        disabled={!canOpen}
        onClick={onOpen}
        aria-label={`Открыть документ: ${artifact.title}`}
        title={canFetch ? "Открыть" : "Файл недоступен"}
        className="flex w-full items-center gap-3 rounded-xl border border-neutral-700/60 bg-neutral-800/50 py-2.5 pl-2.5 pr-28 text-left transition-colors hover:border-neutral-600 hover:bg-neutral-800 active:scale-[0.99] focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60 disabled:cursor-default disabled:active:scale-100 disabled:hover:border-neutral-700/60 disabled:hover:bg-neutral-800/50"
      >
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-neutral-700/60 text-neutral-300">
          {open === "loading" ? <SpinnerGlyph /> : <DocGlyph />}
        </span>
        <span className="min-w-0 flex-1">
          <span
            className="block truncate text-sm font-medium text-neutral-100"
            title={artifact.title}
          >
            {artifact.title}
          </span>
          <span
            className={`block truncate text-xs ${
              errored ? "text-red-400" : "text-neutral-500"
            }`}
          >
            {errored ? errorText : artifact.typeLabel}
          </span>
        </span>
      </button>
      <button
        type="button"
        disabled={!canDownload}
        onClick={(e) => {
          e.stopPropagation();
          void onDownload();
        }}
        aria-label={`Скачать ${artifact.title}`}
        title={canFetch ? "Скачать" : "Файл недоступен"}
        className="absolute right-2 top-1/2 flex -translate-y-1/2 items-center gap-1.5 rounded-lg border border-neutral-700/60 px-2.5 py-1.5 text-xs text-neutral-300 transition-colors hover:border-neutral-600 hover:bg-neutral-700/60 hover:text-neutral-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-neutral-700/60 disabled:hover:bg-transparent disabled:hover:text-neutral-300"
      >
        {download === "loading" ? <SpinnerGlyph /> : <DownloadGlyph />}
        <span className="hidden sm:inline">Скачать</span>
      </button>
    </div>
  );
}
