"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { Button } from "@/components/ui/Button";
import { ToneProvider } from "@/components/ui/Tone";

const COOKIE_NAME = "consent_given";
const ONE_YEAR_SECONDS = 60 * 60 * 24 * 365;

function hasConsentCookie(): boolean {
  if (typeof document === "undefined") return false;
  return document.cookie
    .split(";")
    .some((c) => c.trim().startsWith(`${COOKIE_NAME}=true`));
}

export function CookieConsent() {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (!hasConsentCookie()) setVisible(true);
  }, []);

  const accept = () => {
    document.cookie = `${COOKIE_NAME}=true; Max-Age=${ONE_YEAR_SECONDS}; Path=/; SameSite=Lax`;
    setVisible(false);
  };

  if (!visible) return null;

  return (
    <ToneProvider tone="dark">
      <div
        role="dialog"
        aria-label="Согласие на использование cookies"
        className="fixed inset-x-0 bottom-0 z-20 border-t border-neutral-800 bg-neutral-900 shadow-lg"
      >
        <div className="mx-auto flex max-w-6xl flex-col items-center gap-3 px-4 py-3 text-sm text-neutral-300 sm:flex-row sm:justify-between">
          <p className="text-center sm:text-left">
            Мы используем cookies для работы сервиса.{" "}
            <Link
              href="/legal/cookies"
              className="text-sky-400 underline hover:no-underline"
            >
              Подробнее
            </Link>
          </p>
          <Button size="sm" onClick={accept}>
            Принять
          </Button>
        </div>
      </div>
    </ToneProvider>
  );
}

export default CookieConsent;
