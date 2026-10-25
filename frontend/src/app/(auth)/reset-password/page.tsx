"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";

import { ApiRequestError, apiClient } from "@/lib/api";
import { Spinner } from "@/components/ui/Spinner";
import { ResetPasswordForm } from "./ResetPasswordForm";
import type { ResetStep } from "./ResetPasswordForm";

export default function ResetPasswordPage() {
  return (
    <Suspense fallback={<Spinner size="lg" />}>
      <ResetPasswordInner />
    </Suspense>
  );
}

function ResetPasswordInner() {
  const searchParams = useSearchParams();
  const urlToken = searchParams.get("token");

  const [step, setStep] = useState<ResetStep>(urlToken ? "confirm" : "request");
  const [email, setEmail] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [token, setToken] = useState<string | null>(urlToken);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (urlToken) {
      setToken(urlToken);
      setStep("confirm");
    }
  }, [urlToken]);

  const submitRequest = async () => {
    if (!email.includes("@")) {
      setError("Введите корректный email.");
      return;
    }
    setSubmitting(true);
    setError(null);
    setSuccess(null);
    try {
      await apiClient.post("/api/v1/auth/reset-password/request", { email });
      setSuccess(`Если email зарегистрирован, мы отправили ссылку на ${email}.`);
    } catch (err) {
      setError(translateError(err, "Не удалось отправить письмо."));
    } finally {
      setSubmitting(false);
    }
  };

  const submitConfirm = async () => {
    if (!token) {
      setError("Ссылка для сброса пароля недействительна.");
      return;
    }
    if (newPassword.length < 8) {
      setError("Минимум 8 символов.");
      return;
    }
    setSubmitting(true);
    setError(null);
    setSuccess(null);
    try {
      await apiClient.post("/api/v1/auth/reset-password/confirm", {
        token,
        new_password: newPassword,
      });
      setSuccess("Пароль успешно изменён. Теперь вы можете войти.");
    } catch (err) {
      setError(translateError(err, "Не удалось изменить пароль."));
    } finally {
      setSubmitting(false);
    }
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (step === "request") {
      void submitRequest();
    } else {
      void submitConfirm();
    }
  };

  return (
    <ResetPasswordForm
      step={step}
      email={email}
      newPassword={newPassword}
      error={error}
      success={success}
      isLoading={submitting}
      onEmailChange={setEmail}
      onPasswordChange={setNewPassword}
      onSubmit={handleSubmit}
    />
  );
}

function translateError(err: unknown, fallback: string): string {
  if (err instanceof ApiRequestError) {
    return err.message || fallback;
  }
  return fallback;
}
