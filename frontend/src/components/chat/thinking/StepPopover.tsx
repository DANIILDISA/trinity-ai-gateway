"use client";

import type { ReactNode } from "react";

// Presentational detail panel for a thinking step, rendered in-flow directly
// below its trigger (inside the scrollable thinking body) so it is never clipped
// by the panel's overflow — unlike an absolute overlay, which the panel's
// overflow-hidden / scroll body would cut off. Visibility + dismissal are owned
// by the parent (ToolStepSegment via useDismiss); the parent passes
// aria-labelledby pointing at the title node so it has an accessible name.
export function StepPopover({
  children,
  "aria-labelledby": labelledBy,
}: {
  children: ReactNode;
  "aria-labelledby"?: string;
}) {
  return (
    <div
      role="dialog"
      aria-labelledby={labelledBy}
      className="mt-1 animate-scaleIn overflow-hidden rounded-lg border border-neutral-700 bg-neutral-900"
    >
      {children}
    </div>
  );
}
