"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { apiClient } from "@/lib/api";
import { scheduleTokenRefresh, setAccessToken } from "@/lib/auth";
import type { User } from "@/types/api";
import { Spinner } from "@/components/ui/Spinner";

export default function OAuthCallbackPage() {
  // Next.js 15 requires useSearchParams consumers to be wrapped in Suspense so
  // the static-export check has a boundary to bail out at.
  return (
    <Suspense fallback={<Spinner size="lg" />}>
      <OAuthCallbackInner />
    </Suspense>
  );
}

function OAuthCallbackInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const token = searchParams.get("token");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function finishLogin() {
      if (!token) {
        setError("Не удалось получить токен авторизации.");
        return;
      }
      setAccessToken(token);
      scheduleTokenRefresh();
      try {
        await apiClient.get<User>("/api/v1/users/me");
        if (!cancelled) router.replace("/chat");
      } catch {
        if (!cancelled) {
          setAccessToken(null);
          setError("Не удалось завершить вход. Попробуйте ещё раз.");
        }
      }
    }

    void finishLogin();
    return () => {
      cancelled = true;
    };
  }, [router, token]);

  return (
    <section className="flex flex-col items-center gap-4 rounded-lg border border-neutral-800 bg-neutral-900 p-8 text-center">
      {error ? (
        <>
          <h1 className="text-lg font-semibold text-neutral-100">Ошибка входа</h1>
          <p role="alert" className="text-sm text-red-400">
            {error}
          </p>
        </>
      ) : (
        <>
          <Spinner size="lg" />
          <p className="text-sm text-neutral-300">Выполняется вход...</p>
        </>
      )}
    </section>
  );
}
