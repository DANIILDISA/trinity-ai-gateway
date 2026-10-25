"use client";

import { useEffect } from "react";

import { Spinner } from "@/components/ui/Spinner";

const AUTO_RETRY_MS = 5000;

/**
 * Shown when auth hydration can't reach the backend (network / 5xx) so the
 * session status is unknown. The user is NOT logged out — the persistent refresh
 * cookie is still valid — we just can't confirm it right now (tr-lgm). Retries
 * automatically and offers a manual retry, recovering the moment the backend is
 * reachable again.
 */
export function ReconnectingScreen({ onRetry }: { onRetry: () => void }) {
  useEffect(() => {
    const id = setInterval(onRetry, AUTO_RETRY_MS);
    return () => clearInterval(id);
  }, [onRetry]);

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-4 bg-[var(--trinity-canvas)] px-6 text-center">
      <Spinner size="lg" className="text-emerald-400" />
      <div className="space-y-1">
        <p className="text-sm font-medium text-neutral-200">
          Не удаётся связаться с сервером
        </p>
        <p className="text-sm text-neutral-400">
          Пробуем переподключиться. Вы остаётесь в системе.
        </p>
      </div>
      <button
        type="button"
        onClick={onRetry}
        className="rounded-md border border-emerald-700/60 bg-emerald-600/10 px-4 py-1.5 text-sm font-medium text-emerald-300 transition-colors hover:bg-emerald-600/20"
      >
        Повторить
      </button>
    </main>
  );
}

export default ReconnectingScreen;
