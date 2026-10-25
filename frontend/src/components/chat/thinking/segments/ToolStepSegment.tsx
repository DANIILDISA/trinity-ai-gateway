"use client";

import { useId, useState } from "react";

import type { ToolCallSegment } from "@/types/thinking";
import { ClampedContent } from "../../ClampedContent";
import { CodeHighlight } from "../../CodeHighlight";
import { StepChip } from "../StepChip";
import { RichResults } from "./richResults";

// Best-effort language hint from a filename's extension (e.g. "calc.py" → "py").
// highlight.ts/normalizeLang maps common extensions (py→python, ts→typescript…)
// to Shiki ids, falling back to a plain block when unknown — so passing the raw
// extension is safe.
function langFromFilename(filename?: string): string | undefined {
  if (!filename) return undefined;
  const dot = filename.lastIndexOf(".");
  return dot > 0 ? filename.slice(dot + 1).toLowerCase() : undefined;
}

// Document glyph for the file chip (create_file / str_replace), matching the
// reference tool-block (Claude.ai).
function DocIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <path d="M14 3v5h5" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
    </svg>
  );
}

// The «›» chevron at the END of the step-group header, rotating 90° when open —
// shared visual language with the reasoning segment disclosure (tr-cy0.3.10).
function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      className={`shrink-0 transition-transform duration-200 ${open ? "rotate-90" : ""}`}
    >
      <path
        d="M9 6l6 6-6 6"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// Red alert marker for a failed tool step (tr-cy0.2.14). Labelled (not
// aria-hidden) so the error is announced to assistive tech.
function AlertIcon() {
  return (
    <span
      className="shrink-0 text-red-400"
      role="img"
      aria-label="Ошибка"
      title="Ошибка"
    >
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path
          d="M12 9v4m0 3.5h.01M10.3 4.3 2.5 18a2 2 0 0 0 1.7 3h15.6a2 2 0 0 0 1.7-3L13.7 4.3a2 2 0 0 0-3.4 0z"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  );
}

// Inline diff counter next to the filename chip: +added emerald, -removed red,
// monospace, compact, no border (reference style). Either side omitted when 0.
function DiffCounter({ added, removed }: { added: number; removed: number }) {
  if (added <= 0 && removed <= 0) return null;
  return (
    <span
      className="inline-flex shrink-0 items-center gap-1.5 font-mono text-[11px] leading-none"
      aria-label={`плюс ${added}, минус ${removed}`}
    >
      {added > 0 ? <span className="text-emerald-400">+{added}</span> : null}
      {removed > 0 ? <span className="text-red-400">-{removed}</span> : null}
    </span>
  );
}

// Wall-clock footer for a code run: "выполнено за 1,2 с" (RU decimal comma).
function formatRunDuration(ms?: number): string | null {
  if (ms == null) return null;
  const sec = ms / 1000;
  const txt = sec < 10 ? sec.toFixed(1) : Math.round(sec).toString();
  return `выполнено за ${txt.replace(".", ",")} с`;
}

// The submitted code lives in the tool-call arguments (streamed as a JSON string
// live, or an object). Returns the `code` field, or null when unavailable (e.g. a
// reloaded step that didn't persist the input).
function codeFromInput(input: unknown): string | null {
  if (typeof input === "string") {
    try {
      const parsed = JSON.parse(input) as { code?: unknown };
      return typeof parsed?.code === "string" ? parsed.code : null;
    } catch {
      return null;
    }
  }
  if (input && typeof input === "object" && typeof (input as { code?: unknown }).code === "string") {
    return (input as { code: string }).code;
  }
  return null;
}

// run_python run-card (epic tr-s3y1 Phase 2, §8): the submitted Python (highlighted),
// the captured output (neutral when it ran, red on a traceback), and a footer with
// the wall-clock duration + a truncation marker. Stdout/stderr are combined in
// `output` (what the model saw); the status drives the colour.
function RunPythonBody({ segment }: { segment: ToolCallSegment }) {
  const code = codeFromInput(segment.input);
  const duration = formatRunDuration(segment.duration_ms);
  const isError = segment.status === "error";
  return (
    <div className="mt-1 space-y-1.5">
      {code ? (
        <div className="overflow-hidden rounded-md bg-[#161615]">
          <CodeHighlight language="python" code={code} />
        </div>
      ) : null}
      {segment.output ? (
        <ClampedContent clampPx={140} fadeClass="from-[var(--trinity-canvas)]" hoverReveal>
          <pre
            className={`whitespace-pre-wrap break-words font-mono text-[11px] leading-5 ${
              isError ? "text-red-400" : "text-neutral-300"
            }`}
          >
            {segment.output}
          </pre>
        </ClampedContent>
      ) : null}
      {duration || segment.truncated ? (
        <div className="flex items-center gap-2 text-[11px] text-neutral-500">
          {duration ? <span>{duration}</span> : null}
          {segment.truncated ? <span>· вывод обрезан</span> : null}
        </div>
      ) : null}
      <RichResults results={segment.results} />
    </div>
  );
}

// A single agentic tool step rendered as its OWN disclosure (Claude.ai parity,
// reuse of the reasoning-segment collapse pattern tr-cy0.3.10): a muted action
// title with a trailing chevron, EXPANDED by default so the compact step detail
// is visible at a glance (reference screenshot_3/6). The body holds the monospace
// filename chip with a doc glyph, an inline +N/−M diff counter, and — when the
// tool returned output — a clamped block with a «Показать больше/меньше» toggle.
// Running → pulsing dot in the header; done/error → static.
export function ToolStepSegment({ segment }: { segment: ToolCallSegment }) {
  const [open, setOpen] = useState(true);
  const bodyId = useId();
  // Prefer the filename; when it's absent (e.g. a hard Stop interrupted
  // create_file before the path streamed), fall back to the friendly RU `title`
  // («Создание файла»), never the raw tool name «create_file» (tr-8bj).
  const chipLabel = segment.filename ?? segment.title;

  return (
    <div className="my-1.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={bodyId}
        title={segment.title}
        className="flex w-full items-center gap-1 rounded py-0.5 text-left text-xs font-medium text-neutral-300 transition-colors hover:text-neutral-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/40"
      >
        <span className="min-w-0 flex-1 truncate">{segment.title}</span>
        {segment.status === "running" ? (
          <span
            className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-emerald-500"
            aria-hidden="true"
          />
        ) : segment.status === "error" ? (
          <AlertIcon />
        ) : null}
        <Chevron open={open} />
      </button>
      {/* Smooth per-step expand/collapse (grid-rows 0fr <-> 1fr). */}
      <div
        id={bodyId}
        className="grid transition-all duration-300 ease-out"
        style={{ gridTemplateRows: open ? "1fr" : "0fr", opacity: open ? 1 : 0 }}
      >
        <div className="overflow-hidden">
          {segment.tool === "run_python" ? (
            <RunPythonBody segment={segment} />
          ) : (
          <>
          <div className="mt-1 flex min-w-0 items-center gap-2">
            <StepChip icon={<DocIcon />} label={chipLabel} title={chipLabel} mono />
            {segment.diff ? (
              <DiffCounter added={segment.diff.added} removed={segment.diff.removed} />
            ) : null}
          </div>
          {segment.output ? (
            <div className="group mt-1.5">
              <ClampedContent
                clampPx={140}
                fadeClass="from-[var(--trinity-canvas)]"
                hoverReveal
              >
                {segment.status === "error" ? (
                  // Errors stay muted red plain text — they're diagnostics, not code.
                  <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-5 text-red-400">
                    {segment.output}
                  </pre>
                ) : (
                  // Code output (a viewed/read file) renders as a highlighted code
                  // panel, same surface as the main answer (tr-cy0.3.19). The
                  // filename chip above already labels it, so no extra header.
                  <div className="overflow-hidden rounded-md bg-[#161615]">
                    <CodeHighlight
                      language={langFromFilename(segment.filename)}
                      code={segment.output}
                    />
                  </div>
                )}
              </ClampedContent>
            </div>
          ) : null}
          </>
          )}
        </div>
      </div>
    </div>
  );
}
