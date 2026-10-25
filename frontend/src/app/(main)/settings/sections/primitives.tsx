"use client";

import type { ButtonHTMLAttributes, ReactNode } from "react";

import { Spinner } from "@/components/ui/Spinner";

// Shared scaffolding for the settings sections. Keeps the visual rhythm
// (panel chrome, headers, action button) identical across all six tabs.

export function SectionShell({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-6">
      {/* One heading only — the decorative «// title» eyebrow duplicated the
          section name right above the H2 (tr-a7fg). */}
      <header className="flex flex-col gap-1 border-b border-neutral-800 pb-4">
        <h2 className="text-2xl font-semibold tracking-tight text-neutral-100">
          {title}
        </h2>
        {description ? (
          <p className="text-sm text-neutral-400">{description}</p>
        ) : null}
      </header>
      <div className="flex flex-col gap-5">{children}</div>
    </div>
  );
}

export function Panel({
  title,
  action,
  children,
}: {
  title?: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="overflow-hidden rounded-lg border border-neutral-800 bg-neutral-900">
      {title || action ? (
        <header className="flex items-center justify-between gap-3 border-b border-neutral-800 px-5 py-3">
          {title ? (
            <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-500">
              {title}
            </h3>
          ) : (
            <span />
          )}
          {action}
        </header>
      ) : null}
      <div className="px-5 py-5">{children}</div>
    </section>
  );
}

type AccentButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  loading?: boolean;
};

// Primary CTA in settings. Uses the chat send-button accent (emerald-600)
// so the brand colour stays consistent.
export function AccentButton({
  loading = false,
  disabled,
  className = "",
  type = "button",
  children,
  ...rest
}: AccentButtonProps) {
  const isDisabled = disabled || loading;
  return (
    <button
      type={type}
      disabled={isDisabled}
      className={`inline-flex h-10 items-center justify-center gap-2 rounded-md bg-emerald-600 px-5 text-sm font-medium text-white transition-colors hover:bg-emerald-500 active:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-emerald-600/40 disabled:text-white/70 ${className}`}
      {...rest}
    >
      {loading ? <Spinner size="sm" /> : null}
      {children}
    </button>
  );
}

export function GhostButton({
  className = "",
  type = "button",
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type={type}
      className={`inline-flex h-10 items-center justify-center gap-2 rounded-md border border-neutral-700 bg-neutral-800 px-4 text-sm font-medium text-neutral-200 transition-colors hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-50 ${className}`}
      {...rest}
    >
      {children}
    </button>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <p className="py-4 font-mono text-xs uppercase tracking-wider text-neutral-600">
      {children}
    </p>
  );
}
