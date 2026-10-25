"use client";

import { memo } from "react";

export interface VersionSwitcherProps {
  // 0-based ordinal among siblings. The wire `version_index` is 1-based, so the
  // caller subtracts 1 before passing it here (MessageBubble does this).
  index: number;
  // Total sibling count (matches MessageResponse.version_count).
  count: number;
  onPrev: () => void;
  onNext: () => void;
  className?: string;
}

function Chevron({ dir }: { dir: "left" | "right" }) {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d={dir === "left" ? "M15 6l-6 6 6 6" : "M9 6l6 6-6 6"}
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// Claude-style `‹ n/m ›` version paging. Auto-hides for a single version so
// legacy/un-branched turns (version_count === 1) render nothing. Muted neutral
// styling so it reads as quiet chrome next to the assistant answer / user row;
// brightens on hover, mirroring the Show-more / thinking controls.
function VersionSwitcherInner({
  index,
  count,
  onPrev,
  onNext,
  className,
}: VersionSwitcherProps) {
  if (count <= 1) return null;
  const atStart = index <= 0;
  const atEnd = index >= count - 1;
  return (
    <div
      className={`inline-flex select-none items-center gap-0.5 font-mono text-xs tabular-nums text-neutral-500 ${
        className ?? ""
      }`}
    >
      <button
        type="button"
        onClick={() => {
          if (!atStart) onPrev();
        }}
        disabled={atStart}
        aria-label="Предыдущая версия"
        className="inline-flex h-5 w-5 items-center justify-center rounded transition-colors hover:text-neutral-200 disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:text-neutral-500"
      >
        <Chevron dir="left" />
      </button>
      <span aria-label="Версия">
        {index + 1}/{count}
      </span>
      <button
        type="button"
        onClick={() => {
          if (!atEnd) onNext();
        }}
        disabled={atEnd}
        aria-label="Следующая версия"
        className="inline-flex h-5 w-5 items-center justify-center rounded transition-colors hover:text-neutral-200 disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:text-neutral-500"
      >
        <Chevron dir="right" />
      </button>
    </div>
  );
}

export const VersionSwitcher = memo(VersionSwitcherInner);
VersionSwitcher.displayName = "VersionSwitcher";

export default VersionSwitcher;
