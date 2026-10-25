"use client";

import Link from "next/link";

import { Panel, SectionShell } from "./primitives";

interface LegalLink {
  href: string;
  label: string;
}

// Mirror the footer (components/layout/Footer.tsx). Edit there first, then
// echo the changes here — the footer remains the source of truth and we just
// surface the same documents inside the settings shell.
const LEGAL_LINKS: readonly LegalLink[] = [
  { href: "/legal/terms", label: "Условия использования" },
  { href: "/legal/privacy", label: "Политика конфиденциальности" },
  { href: "/legal/cookies", label: "Cookies" },
];

function ExternalLinkIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M14 4h6v6M20 4L10 14M19 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h5"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function PrivacySection() {
  return (
    <SectionShell
      title="Конфиденциальность"
      description="Правовые документы и политики Trinity."
    >
      <Panel title="Документы">
        <ul className="flex flex-col gap-1">
          {LEGAL_LINKS.map((link) => (
            <li key={link.href}>
              <Link
                href={link.href}
                target="_blank"
                rel="noreferrer"
                className="group flex items-center justify-between rounded-md px-3 py-3 transition-colors hover:bg-neutral-800/60"
              >
                <span className="text-sm text-neutral-200">{link.label}</span>
                <span className="text-neutral-500 transition-colors group-hover:text-emerald-400">
                  <ExternalLinkIcon />
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </Panel>
    </SectionShell>
  );
}
