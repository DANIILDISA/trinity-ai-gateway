"use client";

import { useState, type ReactNode } from "react";

import type { ThinkingProcess as ThinkingProcessType } from "@/types/thinking";
import { getRenderer } from "./registry";
import { ReasoningMarkdown } from "./segments/ReasoningSegment";
import { ThinkingBody } from "./ThinkingBody";
import { ThinkingHeader } from "./ThinkingHeader";
import { useThinkingClock } from "./useThinkingClock";

function ClockIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.6" />
      <path
        d="M12 7.5V12l3 1.8"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.6" />
      <path
        d="M8.5 12.3l2.4 2.4 4.6-5.1"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// Stop glyph for the «Остановлено» terminal node (tr-fo1) — a rounded square
// inside a ring, the universal "stopped" mark. Calm amber (not error red): the
// reply was interrupted, not failed.
function StopIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.6" />
      <rect x="9" y="9" width="6" height="6" rx="1" fill="currentColor" />
    </svg>
  );
}

// Document glyph for the «Файл представлен» rail node (tr-cy0.2.11) — a sheet
// with a folded corner, matching the reference timeline node (screenshot_3).
function FileIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
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

// A terminal node on the timeline rail (clock → … → foot nodes). The icon sits
// on the rail (absolute, canvas-backed so the rail line is masked behind it),
// the muted label follows. Used for «Файл представлен», «Готово», «Остановлено».
// `tone="stopped"` paints it calm amber so a stopped reply reads distinctly from
// a completed one without shouting like an error (tr-fo1).
function FootNode({
  icon,
  children,
  tone = "default",
}: {
  icon: ReactNode;
  children: ReactNode;
  tone?: "default" | "stopped";
}) {
  const rowTone = tone === "stopped" ? "text-amber-400" : "text-neutral-400";
  const iconTone = tone === "stopped" ? "text-amber-400" : "text-neutral-500";
  return (
    <div className={`relative mt-3 flex items-center pl-7 text-xs ${rowTone}`}>
      <span
        className={`absolute left-0 bg-[var(--trinity-canvas)] py-0.5 ${iconTone}`}
        aria-hidden="true"
      >
        {icon}
      </span>
      {children}
    </div>
  );
}

// Distinct files presented this turn (done file-tool steps). Drives the single
// «Файл представлен» node (D7: one presentation per file at session end, not one
// per tool_result), so a create + N edits never flashes the node repeatedly.
function presentedFileCount(process: ThinkingProcessType): number {
  const files = new Set<string>();
  for (const s of process.segments) {
    if (s.kind === "tool_call" && s.status === "done" && s.filename) {
      files.add(s.filename);
    }
  }
  return files.size;
}

// Container for the whole thinking timeline. It never hard-codes segment types —
// each segment is rendered by whatever the registry returns for its kind, so new
// capabilities plug in via register(). The body expands/collapses with a smooth
// grid-rows animation and is laid out as a Claude-style timeline: a vertical
// rail with a clock node at the start and a "Готово" check node at the end.
export function ThinkingProcess({
  process,
}: {
  process: ThinkingProcessType;
}) {
  const [userExpanded, setUserExpanded] = useState(false);
  // Multi-segment whole-block collapse (tr-cy0.3.18). Default EXPANDED so the
  // timeline + tool steps stay visible (tr-ygx); the header chevron collapses it
  // to a single summary line, like Claude.
  const [multiCollapsed, setMultiCollapsed] = useState(false);
  const elapsedMs = useThinkingClock(
    process.isActive,
    process.startedAt,
    process.durationMs,
  );

  const hasContent = process.segments.length > 0;
  if (!process.isActive && !hasContent) return null;

  // «Файл представлен» renders once at session end when ≥1 file was authored.
  const hasPresentedFile = presentedFileCount(process) > 0;

  // Multi-segment timeline (Claude.ai parity): each reasoning segment carries its
  // own title + collapse, so there's no overall ThinkingHeader/ThinkingBody wrap.
  // A single untitled block (legacy) keeps the original container collapse below.
  const reasoningCount = process.segments.filter(
    (s) => s.kind === "reasoning",
  ).length;
  const titledReasoning = process.segments.some(
    (s) => s.kind === "reasoning" && (s.summary !== undefined || s.summaryPending),
  );
  // Tool steps must render inline on the always-visible timeline (with the
  // «Файл представлен»/«Готово» rail nodes) — never buried behind the legacy
  // single-block «Размышление» collapse, even when cross-round reasoning merged
  // into one untitled block (tr-ygx, reference screenshot_3/6).
  const hasToolSteps = process.segments.some((s) => s.kind === "tool_call");
  const multiSegment = reasoningCount > 1 || titledReasoning || hasToolSteps;

  if (multiSegment) {
    // tr-cy0.3.18: a whole-block collapse, default EXPANDED. The header carries
    // the duration+token meta (so the collapsed one-line is a real summary), so
    // the terminal foot node stays bare — no duplicated meta.
    const open = !multiCollapsed;
    const canToggle = hasContent;
    return (
      <div>
        <ThinkingHeader
          isActive={process.isActive}
          elapsedMs={elapsedMs}
          durationMs={process.durationMs}
          reasoningTokens={process.reasoningTokens}
          open={open}
          canToggle={canToggle}
          onToggle={() => setMultiCollapsed((v) => !v)}
        />
        {/* Smooth whole-block expand/collapse (grid-rows 0fr <-> 1fr). Default
            open so the agentic evidence shows without a click (tr-ygx). */}
        <div
          className="grid transition-all duration-300 ease-out"
          style={{ gridTemplateRows: open ? "1fr" : "0fr", opacity: open ? 1 : 0 }}
        >
          <div className="overflow-hidden">
            <div className="relative pt-1">
              {/* Vertical rail connecting the clock and check nodes. */}
              <span
                className="absolute bottom-3 left-[7px] top-3 w-px bg-neutral-700/60"
                aria-hidden="true"
              />
              {/* Clock node + the stack of self-collapsing per-segment headers. */}
              <div className="relative pl-7">
                <span
                  className="absolute left-0 top-1 bg-[var(--trinity-canvas)] py-0.5 text-neutral-500"
                  aria-hidden="true"
                >
                  <ClockIcon />
                </span>
                <div className="space-y-1">
                  {process.segments.map((segment) => {
                    const Renderer = getRenderer(segment.kind);
                    return Renderer ? (
                      <Renderer key={segment.id} segment={segment} />
                    ) : null;
                  })}
                </div>
              </div>
              {/* «Файл представлен» (if a file was authored) then the terminal
                  node — «Остановлено» (amber) for a stopped reply, else «Готово».
                  Bare: the meta lives on the header above. */}
              {!process.isActive ? (
                <>
                  {hasPresentedFile ? (
                    <FootNode icon={<FileIcon />}>Файл представлен</FootNode>
                  ) : null}
                  {process.stopped ? (
                    <FootNode icon={<StopIcon />} tone="stopped">
                      Остановлено
                    </FootNode>
                  ) : (
                    <FootNode icon={<CheckIcon />}>Готово</FootNode>
                  )}
                </>
              ) : null}
            </div>
          </div>
        </div>
      </div>
    );
  }

  const open = process.isActive || userExpanded;
  const canToggle = !process.isActive && hasContent;

  // Reveal only the last reasoning block while thinking is active, so finished
  // earlier blocks don't re-animate.
  const lastReasoningId = [...process.segments]
    .reverse()
    .find((s) => s.kind === "reasoning")?.id;

  return (
    <div>
      <ThinkingHeader
        isActive={process.isActive}
        elapsedMs={elapsedMs}
        durationMs={process.durationMs}
        reasoningTokens={process.reasoningTokens}
        open={open}
        canToggle={canToggle}
        onToggle={() => setUserExpanded((v) => !v)}
      />
      {/* Smooth expand/collapse: animate grid-template-rows 0fr <-> 1fr. */}
      <div
        className="grid transition-all duration-300 ease-out"
        style={{ gridTemplateRows: open ? "1fr" : "0fr", opacity: open ? 1 : 0 }}
      >
        <div className="overflow-hidden">
          {hasContent ? (
            <div className="relative pt-1">
              {/* Vertical rail connecting the clock and check nodes. */}
              <span
                className="absolute bottom-3 left-[7px] top-3 w-px bg-neutral-700/60"
                aria-hidden="true"
              />
              {/* Clock node + reasoning. */}
              <div className="relative pl-7">
                <span
                  className="absolute left-0 top-1 bg-[var(--trinity-canvas)] py-0.5 text-neutral-500"
                  aria-hidden="true"
                >
                  <ClockIcon />
                </span>
                <ThinkingBody active={process.isActive}>
                  {process.segments.map((segment) => {
                    // Legacy path renders the single reasoning block bare (no
                    // per-segment header); the container provides the collapse.
                    if (segment.kind === "reasoning") {
                      return (
                        <ReasoningMarkdown
                          key={segment.id}
                          text={segment.text}
                          streaming={process.isActive && segment.id === lastReasoningId}
                        />
                      );
                    }
                    const Renderer = getRenderer(segment.kind);
                    return Renderer ? (
                      <Renderer key={segment.id} segment={segment} />
                    ) : null;
                  })}
                </ThinkingBody>
              </div>
              {/* «Файл представлен» (if authored) then the terminal node —
                  «Остановлено» (amber) for a stopped reply, else «Готово». */}
              {!process.isActive ? (
                <>
                  {hasPresentedFile ? (
                    <FootNode icon={<FileIcon />}>Файл представлен</FootNode>
                  ) : null}
                  {process.stopped ? (
                    <FootNode icon={<StopIcon />} tone="stopped">
                      Остановлено
                    </FootNode>
                  ) : (
                    <FootNode icon={<CheckIcon />}>Готово</FootNode>
                  )}
                </>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
