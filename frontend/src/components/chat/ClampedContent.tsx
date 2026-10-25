"use client";

import type { ReactNode } from "react";

import { ShowMoreToggle } from "./ShowMoreToggle";
import { useOverflowClamp } from "./useOverflowClamp";

// Clamps long content to `clampPx` with a bottom fade into the surrounding
// surface (`fadeClass`, e.g. "from-neutral-800") and a Show more/less toggle —
// no internal scrollbar. `hoverReveal` makes the toggle appear only on `.group`
// hover (thinking); otherwise it is always visible but dim (messages).
// `disabled` skips clamping entirely (e.g. while a stream is active).
export function ClampedContent({
  clampPx,
  fadeClass,
  hoverReveal,
  disabled,
  children,
}: {
  clampPx: number;
  fadeClass: string;
  hoverReveal?: boolean;
  disabled?: boolean;
  children: ReactNode;
}) {
  const { ref, clamped, overflowing, expanded, toggle } = useOverflowClamp(
    clampPx,
    disabled,
  );
  return (
    <>
      <div
        ref={ref}
        className="relative overflow-hidden"
        style={clamped ? { maxHeight: clampPx } : undefined}
      >
        {children}
        {clamped ? (
          <div
            className={`pointer-events-none absolute inset-x-0 bottom-0 h-12 bg-gradient-to-t ${fadeClass} to-transparent`}
          />
        ) : null}
      </div>
      {overflowing ? (
        <ShowMoreToggle
          expanded={expanded}
          onToggle={toggle}
          hoverReveal={hoverReveal}
        />
      ) : null}
    </>
  );
}
