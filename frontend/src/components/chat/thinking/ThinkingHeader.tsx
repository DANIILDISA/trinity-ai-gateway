"use client";

import { buildHeaderMeta, formatDurationSec } from "./formatThinking";

function SparkIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M12 3l1.8 4.7L18.5 9.5 13.8 11.3 12 16l-1.8-4.7L5.5 9.5l4.7-1.8L12 3z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// Trailing «›» chevron, rotating 90° when open — the collapse affordance, shared
// visual language with the per-segment headers (tr-cy0.3.18).
function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      className={`shrink-0 text-neutral-500 transition-transform duration-200 ${
        open ? "rotate-90" : ""
      }`}
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

// The clickable summary row above the thinking body. While active it shows a
// pulsing emerald dot + a live "Размышляю… Nс" counter; when done it collapses
// to "Размышление · Nс · M токенов". A trailing chevron signals the collapse
// (shown only when the row can toggle); the body expands with a smooth
// animation (Claude-style).
export function ThinkingHeader({
  isActive,
  elapsedMs,
  durationMs,
  reasoningTokens,
  open,
  canToggle,
  onToggle,
}: {
  isActive: boolean;
  elapsedMs: number;
  durationMs?: number;
  reasoningTokens?: number;
  open: boolean;
  canToggle: boolean;
  onToggle: () => void;
}) {
  const meta = isActive ? "" : buildHeaderMeta({ durationMs, reasoningTokens });

  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      disabled={!canToggle}
      className={`group/th flex items-center gap-2 px-0.5 py-1 text-left text-xs font-medium ${
        canToggle ? "" : "cursor-default"
      }`}
    >
      {isActive ? (
        <span
          className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500"
          aria-hidden="true"
        />
      ) : (
        <span className="text-emerald-400" aria-hidden="true">
          <SparkIcon />
        </span>
      )}
      <span
        className={`text-neutral-300 transition-colors ${
          canToggle ? "group-hover/th:text-neutral-100" : ""
        }`}
      >
        {isActive ? `Размышляю… ${formatDurationSec(elapsedMs)}` : "Размышление"}
        {meta ? <span className="text-neutral-500"> · {meta}</span> : null}
      </span>
      {canToggle ? <Chevron open={open} /> : null}
    </button>
  );
}
