"use client";

import { useId, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";

import type { ReasoningSegment as ReasoningSegmentType } from "@/types/thinking";
import { ClampedContent } from "../../ClampedContent";
import { CodeBlock } from "../../Markdown";
import { rehypeRevealFade } from "../../streaming/rehypeRevealFade";
import { useStreamReveal } from "../../streaming/useStreamReveal";
import { segmentTitle } from "../segmentTitle";

// Muted markdown body, secondary to the answer. `prose-sm` supplies compact
// spacing; `text-xs` intentionally overrides its font size.
const PROSE =
  "prose prose-invert prose-sm max-w-none break-words text-xs leading-5 text-neutral-400 prose-p:my-1.5 prose-headings:text-neutral-300 prose-strong:text-neutral-200 prose-code:before:hidden prose-code:after:hidden prose-pre:bg-neutral-950 prose-pre:text-neutral-300";

// Code inside reasoning renders like the main answer (tr-cy0.3.19): fenced blocks
// become highlighted CodeBlock panels, inline code a quiet mono chip — instead of
// the default prose <pre> that read as undistinguished plain text.
const REASONING_COMPONENTS: Components = {
  pre({ children }) {
    return <>{children}</>;
  },
  code({ className, children }) {
    const text = String(children ?? "");
    const match = /language-(\w+)/.exec(className ?? "");
    const isBlock = !!match || text.includes("\n");
    if (isBlock) {
      return <CodeBlock language={match?.[1]} code={text.replace(/\n$/, "")} />;
    }
    return (
      <code className="rounded-[5px] bg-white/[0.08] px-1.5 py-0.5 font-mono text-[0.85em] text-neutral-300">
        {children}
      </code>
    );
  },
};

// Bare reasoning markdown (no header). Used directly by the legacy single-block
// container (ThinkingProcess) and as the body of a titled segment.
export function ReasoningMarkdown({
  text,
  streaming = false,
}: {
  text: string;
  streaming?: boolean;
}) {
  const { revealed, revealedCount } = useStreamReveal(text, streaming);
  // Fade stays active through the post-completion drain (buffer still behind the
  // text), so live thinking eases its tail in instead of snapping when it ends.
  const revealing = streaming || revealed.length < text.length;
  return (
    <div className={PROSE}>
      <ReactMarkdown
        components={REASONING_COMPONENTS}
        rehypePlugins={
          (revealing
            ? [[rehypeRevealFade, { revealCount: revealedCount }]]
            : []) as never
        }
      >
        {revealing ? revealed : text}
      </ReactMarkdown>
    </div>
  );
}

// The «›» chevron at the END of the title row, rotating 90° when open.
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

// A reasoning segment as its OWN disclosure (Claude.ai parity): a muted one-line
// summary header with a trailing chevron, COLLAPSED by default; clicking the row
// toggles the reasoning body, which clamps long content with a fade + Show
// more/less. The header text is the summary, falling back to a text-derived
// title (segmentTitle) when no summary is present — so a reloaded / post-Stop
// segment shows a real title, never a stuck skeleton. The pulse skeleton appears
// ONLY while a summary is still being generated (summaryPending && !summary).
//
// This is the multi-segment unit; the legacy single flat block is rendered bare
// by the container via ReasoningMarkdown.
export function ReasoningSegment({
  segment,
}: {
  segment: ReasoningSegmentType;
}) {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  const showSkeleton = segment.summaryPending && !segment.summary;

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={bodyId}
        className="flex w-full items-center gap-1 rounded py-0.5 text-left text-xs font-medium text-neutral-400 transition-colors hover:text-neutral-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/40"
      >
        {showSkeleton ? (
          <span
            className="h-3 w-3/5 animate-pulse rounded bg-neutral-700/60"
            aria-label="Готовится заголовок"
            aria-busy="true"
          />
        ) : (
          <span className="min-w-0 flex-1 truncate">{segmentTitle(segment)}</span>
        )}
        <Chevron open={open} />
      </button>
      {/* Smooth per-segment expand/collapse (grid-rows 0fr <-> 1fr). */}
      <div
        id={bodyId}
        className="grid transition-all duration-300 ease-out"
        style={{ gridTemplateRows: open ? "1fr" : "0fr", opacity: open ? 1 : 0 }}
      >
        <div className="overflow-hidden">
          <div className="group pt-1">
            <ClampedContent
              clampPx={224}
              fadeClass="from-[var(--trinity-canvas)]"
              hoverReveal
            >
              <ReasoningMarkdown text={segment.text} />
            </ClampedContent>
          </div>
        </div>
      </div>
    </div>
  );
}
