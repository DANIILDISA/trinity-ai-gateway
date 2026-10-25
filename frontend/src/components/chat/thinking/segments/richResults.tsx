"use client";

// Rich-output renderers for the run_python card (epic tr-s3y1 Phase 4, tr-5ktl).
// A registry dispatches one renderer per result `type`; new output kinds are added by
// extending RENDERERS, nothing else changes. matplotlib PNGs render inline (live: the
// streamed base64; on reload: re-fetched from the persisted artifact). DataFrame HTML
// is parsed into a plain React table (TEXT cells only — XSS-safe, no innerHTML).

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";

import { base64ToBlob } from "@/lib/binaryBlob";
import { downloadToolArtifact, fetchToolArtifact, saveBlob } from "@/lib/downloadToolArtifact";
import { formatFileSize } from "@/lib/fileTypes";
import type { RichResult } from "@/types/thinking";

function ImageResult({ result }: { result: RichResult }) {
  const params = useParams<{ id?: string }>();
  const [src, setSrc] = useState<string | null>(
    result.data ? `data:image/png;base64,${result.data}` : null,
  );

  useEffect(() => {
    if (result.data) {
      setSrc(`data:image/png;base64,${result.data}`);
      return;
    }
    // Reload path: the base64 isn't persisted — re-fetch the PNG from its artifact
    // (keyed by conversation + filename), then render + download it as a data URI.
    const filename = result.filename;
    const conversationId = params?.id;
    if (!filename || !conversationId) return;
    let cancelled = false;
    fetchToolArtifact(conversationId, filename)
      .then((a) => {
        if (!cancelled) setSrc(`data:${a.media_type};base64,${a.content}`);
      })
      .catch(() => {
        /* a missing/forbidden artifact just renders nothing — never throws */
      });
    return () => {
      cancelled = true;
    };
  }, [result.data, result.filename, params?.id]);

  if (!src) return null;
  return (
    <a href={src} download={result.filename ?? "plot.png"} title="Скачать изображение">
      <img
        src={src}
        alt="Результат выполнения кода"
        className="max-w-full rounded-md border border-neutral-700 bg-white"
      />
    </a>
  );
}

// Parse a DataFrame's _repr_html_ into header/body text rows — TEXT content only, so a
// forged _repr_html_ (prompt-injection) can't inject markup/script (no innerHTML).
function parseTable(html: string): { head: string[][]; body: string[][] } | null {
  if (typeof DOMParser === "undefined") return null;
  const table = new DOMParser().parseFromString(html, "text/html").querySelector("table");
  if (!table) return null;
  const rowsOf = (scope: Element | null): string[][] =>
    scope
      ? Array.from(scope.querySelectorAll(":scope > tr")).map((tr) =>
          Array.from(tr.querySelectorAll("th,td")).map((c) => c.textContent ?? ""),
        )
      : [];
  let head = rowsOf(table.querySelector("thead"));
  let body = rowsOf(table.querySelector("tbody"));
  if (head.length === 0 && body.length === 0) {
    const all = Array.from(table.querySelectorAll("tr")).map((tr) =>
      Array.from(tr.querySelectorAll("th,td")).map((c) => c.textContent ?? ""),
    );
    head = all.slice(0, 1);
    body = all.slice(1);
  }
  return { head, body };
}

function DataFrameResult({ result }: { result: RichResult }) {
  const parsed = result.data ? parseTable(result.data) : null;
  if (!parsed) return null;
  const cell =
    "border border-neutral-700 px-2 py-1 text-left whitespace-nowrap";
  return (
    <div className="overflow-x-auto">
      <table className="border-collapse text-[11px] text-neutral-300">
        {parsed.head.length > 0 ? (
          <thead className="text-neutral-400">
            {parsed.head.map((row, r) => (
              <tr key={r}>
                {row.map((c, i) => (
                  <th key={i} className={`${cell} font-medium`}>
                    {c}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
        ) : null}
        <tbody>
          {parsed.body.map((row, r) => (
            <tr key={r}>
              {row.map((c, i) => (
                <td key={i} className={cell}>
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TextResult({ result }: { result: RichResult }) {
  if (!result.data) return null;
  return (
    <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-5 text-neutral-300">
      {result.data}
    </pre>
  );
}

function FileChipIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true" className="shrink-0">
      <path
        d="M13 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-6-6z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <path d="M13 3v6h6" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
    </svg>
  );
}

// A generated workspace file (Phase 5, tr-an0g): a download chip. Live, the manager
// streamed the base64 bytes in `data`; on reload that base64 was slimmed out of the row,
// so the file is re-fetched from its linked artifact (keyed by conversation + filename).
function FileDownloadChip({ result }: { result: RichResult }) {
  const params = useParams<{ id?: string }>();
  const [busy, setBusy] = useState(false);
  const filename = result.filename ?? "файл";
  // Downloadable when we have inline bytes (live) or a linked artifact (reload). An
  // over-quota file slimmed with no artifact (audit B4) is neither → render disabled.
  const downloadable = Boolean(result.data || result.artifact_id);

  const handleDownload = async () => {
    if (busy || !downloadable) return;
    setBusy(true);
    try {
      if (result.data) {
        // Live: the manager base64-encodes every file's bytes → decode to the real bytes.
        saveBlob(
          base64ToBlob(result.data, result.media_type ?? "application/octet-stream"),
          filename,
        );
      } else if (params?.id) {
        // Reload: re-fetch from the artifact (text stored raw, binary base64 — handled by
        // the shared download path).
        await downloadToolArtifact(params.id, filename);
      }
    } catch {
      /* a missing/forbidden artifact just no-ops — never throws into render */
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      type="button"
      onClick={handleDownload}
      disabled={busy || !downloadable}
      title={downloadable ? `Скачать ${filename}` : `${filename} — недоступен после перезагрузки`}
      className="group inline-flex max-w-full items-center gap-2 rounded-md border border-neutral-700 bg-neutral-800 px-2.5 py-1.5 text-left text-[12px] text-neutral-300 transition-colors hover:border-emerald-500/50 hover:bg-neutral-700/60 disabled:cursor-not-allowed disabled:opacity-50"
    >
      <span className="text-neutral-400 group-hover:text-emerald-400">
        <FileChipIcon />
      </span>
      <span className="truncate">{filename}</span>
      {typeof result.size === "number" && result.size > 0 ? (
        <span className="shrink-0 text-neutral-500">{formatFileSize(result.size)}</span>
      ) : null}
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        aria-hidden="true"
        className="ml-0.5 shrink-0 text-neutral-500 group-hover:text-emerald-400"
      >
        <path d="M12 3v12m0 0l-4-4m4 4l4-4M5 21h14" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </button>
  );
}

const RENDERERS: Record<RichResult["type"], (r: RichResult) => React.ReactNode> = {
  "image/png": (r) => <ImageResult result={r} />,
  "text/html": (r) => <DataFrameResult result={r} />,
  "text/plain": (r) => <TextResult result={r} />,
  file: (r) => <FileDownloadChip result={r} />,
};

export function RichResults({ results }: { results?: RichResult[] }) {
  if (!results?.length) return null;
  return (
    <div className="mt-1.5 flex flex-col gap-2">
      {results.map((r, i) => (
        <div key={i}>{RENDERERS[r.type]?.(r) ?? null}</div>
      ))}
    </div>
  );
}
