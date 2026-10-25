"use client";

import type { ReactNode } from "react";

// A small pill that surfaces a sub-action / detail of a thinking step (e.g. a
// filename or tool name). Interactive (a button, emerald when active) when given
// an `onClick`; otherwise a non-interactive `span` so a purely-visual chip
// inside a disclosure doesn't become a focusable no-op (tr-cy0.2.12).
export function StepChip({
  icon,
  label,
  active,
  onClick,
  mono,
  title,
}: {
  icon?: ReactNode;
  label: string;
  active?: boolean;
  onClick?: () => void;
  // Monospace label for filename chips (tr-cy0.2.10).
  mono?: boolean;
  // Native tooltip so a truncated long filename stays recoverable (tr-cy0.2.15).
  title?: string;
}) {
  const className = `inline-flex max-w-full items-center gap-1.5 rounded-md border px-2 py-1 text-xs font-medium transition-colors ${
    active
      ? "border-emerald-600 bg-emerald-600/15 text-emerald-300"
      : `border-neutral-700 bg-neutral-800 text-neutral-300${
          onClick ? " hover:border-neutral-600 hover:bg-neutral-700" : ""
        }`
  }`;
  const inner = (
    <>
      {icon ? (
        <span className="shrink-0 text-emerald-400" aria-hidden="true">
          {icon}
        </span>
      ) : null}
      <span className={mono ? "min-w-0 truncate font-mono" : "min-w-0 truncate"}>
        {label}
      </span>
    </>
  );

  if (!onClick) {
    return (
      <span className={className} title={title}>
        {inner}
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      title={title}
      className={className}
    >
      {inner}
    </button>
  );
}
