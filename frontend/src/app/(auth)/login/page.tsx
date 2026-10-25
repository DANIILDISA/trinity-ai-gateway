"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { ApiRequestError } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { useAuth } from "@/hooks/useAuth";
import { LoginForm } from "./LoginForm";
import { RecoveryPrompt } from "./RecoveryPrompt";

export default function LoginPage() {
  const router = useRouter();
  const toast = useToast();
  const { isAuthenticated, isLoading: authLoading, login, recover } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // When the backend reports the credentials are valid but the account is
  // pending deletion, swap the form for the recovery confirmation prompt.
  const [pendingDeletion, setPendingDeletion] = useState(false);

  useEffect(() => {
    if (!authLoading && isAuthenticated) {
      router.replace("/chat");
    }
  }, [authLoading, isAuthenticated, router]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password);
    } catch (err) {
      if (err instanceof Error && /account_pending_deletion/.test(err.message)) {
        setPendingDeletion(true);
      } else if (
        err instanceof ApiRequestError &&
        err.code === "account_pending_deletion"
      ) {
        setPendingDeletion(true);
      } else {
        // The translateAuthError flow can also strip the code. Re-check by
        // inspecting the raw message text for the Russian copy.
        const message = err instanceof Error ? err.message : "Не удалось войти.";
        if (message.includes("Аккаунт помечен на удаление")) {
          setPendingDeletion(true);
        } else {
          setError(message);
        }
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleRecover = async () => {
    setError(null);
    setSubmitting(true);
    try {
      await recover(email, password);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось восстановить.");
    } finally {
      setSubmitting(false);
    }
  };

  const handleCancelRecover = () => {
    setPendingDeletion(false);
    setError(null);
  };

  // Yandex OAuth isn't wired yet — the backend route 404s. Don't navigate;
  // tell the user it's coming (BUG 16).
  const handleYandexLogin = () => {
    toast.info("Вход через Яндекс находится в разработке");
  };

  return (
    <div className="flex flex-col gap-4">
      <Link
        href="/"
        className="text-sm text-neutral-600 hover:text-neutral-900 hover:underline"
      >
        ← На главную
      </Link>
      {pendingDeletion ? (
        <RecoveryPrompt
          email={email}
          error={error}
          isLoading={submitting}
          onRecover={() => void handleRecover()}
          onCancel={handleCancelRecover}
        />
      ) : (
        <LoginForm
          email={email}
          password={password}
          error={error}
          isLoading={submitting}
          onEmailChange={setEmail}
          onPasswordChange={setPassword}
          onSubmit={handleSubmit}
          onYandexLogin={handleYandexLogin}
        />
      )}
    </div>
  );
}
