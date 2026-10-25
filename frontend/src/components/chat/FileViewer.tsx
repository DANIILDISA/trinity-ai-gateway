"use client";

import { useEffect, useState } from "react";

import { apiClient } from "@/lib/api";
import type { FileDetail, FileKind } from "@/types/api";

// What the viewer/downloader needs to resolve a file's bytes. A just-sent file
// already has a session object URL; a persisted file is fetched on demand by id.
export type FileTarget = {
  filename: string;
  kind: FileKind;
  mediaType: string;
} & (
  | { source: "local"; blobUrl: string }
  | { source: "persisted"; fileId: string }
);

// File extensions that open in a modal viewer rather than downloading. PDFs use
// a native browser <iframe>; text/code files render in a scrollable code block.
export function isViewableKind(kind: FileKind): boolean {
  return kind === "pdf" || kind === "text";
}

function base64ToBlob(base64: string, mime: string): Blob {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return new Blob([bytes], { type: mime || "application/octet-stream" });
}

// Resolve a target to an object URL (and whether we created it, so the caller
// can revoke). Local targets reuse their existing blobUrl; persisted targets
// fetch the payload and build a fresh blob URL.
async function resolveObjectUrl(
  target: FileTarget,
): Promise<{ url: string; created: boolean }> {
  if (target.source === "local") {
    return { url: target.blobUrl, created: false };
  }
  const detail = await apiClient.get<FileDetail>(
    `/api/v1/files/${target.fileId}`,
  );
  const blob = base64ToBlob(detail.base64_data, detail.media_type);
  return { url: URL.createObjectURL(blob), created: true };
}

function triggerDownload(url: string, filename: string) {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// Resolve + download a file directly (used for non-viewable kinds and the
// modal's download button).
export async function downloadFile(target: FileTarget): Promise<void> {
  const { url, created } = await resolveObjectUrl(target);
  triggerDownload(url, target.filename);
  // Give the browser a tick to start the download before revoking.
  if (created) setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

function CloseIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

function DownloadIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
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

// Inline modal that shows a PDF (native iframe) or a text/code file (scrollable
// code block). Follows DESIGN.md: bg-neutral-900 panel, border-neutral-800,
// animate-scaleIn. Closes on Escape or backdrop click.
export function FileViewerModal({
  target,
  onClose,
}: {
  target: FileTarget;
  onClose: () => void;
}) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    let cancelled = false;
    let createdUrl: string | null = null;
    (async () => {
      try {
        const { url, created } = await resolveObjectUrl(target);
        if (created) createdUrl = url;
        if (cancelled) {
          if (created) URL.revokeObjectURL(url);
          return;
        }
        setObjectUrl(url);
        if (target.kind === "text") {
          const res = await fetch(url);
          const body = await res.text();
          if (!cancelled) setText(body);
        }
      } catch {
        if (!cancelled) setError("Не удалось загрузить файл.");
      }
    })();
    return () => {
      cancelled = true;
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [target]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-neutral-950/80 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={target.filename}
      onClick={onClose}
    >
      <div
        className="flex h-[85vh] w-full max-w-3xl animate-scaleIn flex-col overflow-hidden rounded-lg border border-neutral-800 bg-neutral-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-center justify-between gap-3 border-b border-neutral-800 px-4 py-3">
          <span
            className="min-w-0 flex-1 truncate text-sm font-medium text-neutral-100"
            title={target.filename}
          >
            {target.filename}
          </span>
          <div className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              onClick={() => void downloadFile(target)}
              title="Скачать"
              aria-label="Скачать файл"
              className="inline-flex h-8 w-8 items-center justify-center rounded-md text-neutral-400 transition-colors hover:bg-neutral-800 hover:text-neutral-100"
            >
              <DownloadIcon />
            </button>
            <button
              type="button"
              onClick={onClose}
              title="Закрыть"
              aria-label="Закрыть"
              className="inline-flex h-8 w-8 items-center justify-center rounded-md text-neutral-400 transition-colors hover:bg-neutral-800 hover:text-neutral-100"
            >
              <CloseIcon />
            </button>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-hidden bg-neutral-950">
          {error ? (
            <div className="flex h-full items-center justify-center px-4 text-sm text-red-400">
              {error}
            </div>
          ) : target.kind === "pdf" ? (
            objectUrl ? (
              <iframe
                src={objectUrl}
                title={target.filename}
                className="h-full w-full border-0"
              />
            ) : (
              <Loading />
            )
          ) : text !== null ? (
            <pre className="h-full overflow-auto p-4 text-xs leading-relaxed text-neutral-100">
              <code>{text}</code>
            </pre>
          ) : (
            <Loading />
          )}
        </div>
      </div>
    </div>
  );
}

function Loading() {
  return (
    <div className="flex h-full items-center justify-center text-sm text-neutral-500">
      Загрузка…
    </div>
  );
}
