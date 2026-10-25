"use client";

import { type ReactNode } from "react";

/**
 * Small dark hover tooltip used for the icon-only meta rows. Pure CSS
 * (no portal): the trigger is `group/tt`, the bubble fades + lifts in on
 * `group-hover/tt`. Sits above the trigger, centered, with a matching arrow.
 * `pointer-events-none` so it never eats the hover.
 *
 * Hover-only by design: an earlier `group-focus-within/tt` show-trigger kept
 * the bubble visible after a *click* on a focusable trigger (the wrapper held
 * focus-within until focus moved away), so tooltips got stuck and overlapped
 * each other. Driving visibility purely off hover guarantees the bubble clears
 * the instant the pointer leaves.
 *
 * Design: neutral-900 panel, neutral-700 border, 11px text (DESIGN.md dark
 * scale), soft black shadow — quiet enough to match the chrome.
 */
export function Tooltip({
  label,
  children,
  side = "top",
  className = "",
}: {
  label: ReactNode;
  children: ReactNode;
  side?: "top" | "bottom";
  className?: string;
}) {
  const isTop = side === "top";
  return (
    <span className={`group/tt relative inline-flex items-center ${className}`}>
      {children}
      <span
        role="tooltip"
        className={[
          "pointer-events-none absolute left-1/2 z-50 -translate-x-1/2",
          isTop ? "bottom-full mb-1.5 translate-y-1" : "top-full mt-1.5 -translate-y-1",
          "whitespace-nowrap rounded-md border border-neutral-700 bg-neutral-900",
          "px-2 py-1 text-[11px] font-normal leading-tight text-neutral-200",
          "opacity-0 shadow-lg shadow-black/40 transition-all duration-150",
          "group-hover/tt:translate-y-0 group-hover/tt:opacity-100",
        ].join(" ")}
      >
        {label}
        <span
          className={[
            "absolute left-1/2 h-2 w-2 -translate-x-1/2 rotate-45 bg-neutral-900",
            isTop
              ? "top-full -translate-y-1 border-b border-r border-neutral-700"
              : "bottom-full translate-y-1 border-l border-t border-neutral-700",
          ].join(" ")}
        />
      </span>
    </span>
  );
}
