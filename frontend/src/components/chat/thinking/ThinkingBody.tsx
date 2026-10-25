"use client";

import type { ReactNode } from "react";

import { ClampedContent } from "../ClampedContent";

// Wraps the thinking segments. Long content is clamped to a preview that fades
// into the canvas, with a Show more/less toggle that reveals on hover of the
// thinking block — never an internal scrollbar. While active (streaming) the
// content flows freely so the live stream is fully visible.
const CLAMP_PX = 224;

export function ThinkingBody({
  active,
  children,
}: {
  active: boolean;
  children: ReactNode;
}) {
  return (
    <div className="group pt-1">
      <ClampedContent
        clampPx={CLAMP_PX}
        fadeClass="from-[var(--trinity-canvas)]"
        hoverReveal
        disabled={active}
      >
        <div className="space-y-1">{children}</div>
      </ClampedContent>
    </div>
  );
}
