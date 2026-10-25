"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { apiClient } from "@/lib/api";
import {
  getAccessToken,
  refreshAccessToken,
  scheduleTokenRefresh,
} from "@/lib/auth";
import type { User } from "@/types/api";

type HealthState = "loading" | "ok" | "error";

export default function HomePage() {
  const router = useRouter();
  const [state, setState] = useState<HealthState>("loading");
  const [checkingAuth, setCheckingAuth] = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function checkAuthAndRedirect() {
      let token = getAccessToken();
      if (!token) {
        const outcome = await refreshAccessToken();
        token = outcome === "refreshed" ? getAccessToken() : null;
      } else {
        scheduleTokenRefresh();
      }

      if (token) {
        try {
          await apiClient.get<User>("/api/v1/users/me");
          if (!cancelled) router.replace("/chat");
          return;
        } catch {
          // fall through to landing
        }
      }
      if (!cancelled) setCheckingAuth(false);
    }

    void checkAuthAndRedirect();
    return () => {
      cancelled = true;
    };
  }, [router]);

  useEffect(() => {
    if (checkingAuth) return;
    const baseUrl =
      process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
    fetch(`${baseUrl}/api/v1/health`, { cache: "no-store" })
      .then((res) => res.json())
      .then((data: { status?: string }) => {
        setState(data.status === "healthy" ? "ok" : "error");
      })
      .catch(() => setState("error"));
  }, [checkingAuth]);

  if (checkingAuth) {
    return (
      <main className="flex min-h-screen items-center justify-center px-6">
        <p className="text-sm text-neutral-500">Загрузка...</p>
      </main>
    );
  }

  const label =
    state === "loading"
      ? "проверка..."
      : state === "ok"
        ? "OK"
        : "недоступен";

  const styles =
    state === "loading"
      ? "border-neutral-800 bg-neutral-900 text-neutral-300"
      : state === "ok"
        ? "border-emerald-800/50 bg-emerald-950/40 text-emerald-300"
        : "border-red-800 bg-red-900/30 text-red-400";

  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col items-start justify-center gap-6 px-6 py-12">
      <h1 className="text-4xl font-semibold tracking-tight text-neutral-100">
        Trinity
      </h1>
      <p className="text-neutral-400">AI-модели за рубли.</p>

      <div className={`rounded border px-4 py-2 text-sm ${styles}`}>
        Backend: {label}
      </div>

      <nav className="flex gap-4 text-sm">
        <Link className="text-sky-400 hover:underline" href="/login">
          Войти
        </Link>
        <Link className="text-sky-400 hover:underline" href="/register">
          Зарегистрироваться
        </Link>
        <Link className="text-sky-400 hover:underline" href="/pricing">
          Посмотреть тарифы →
        </Link>
      </nav>
    </main>
  );
}
