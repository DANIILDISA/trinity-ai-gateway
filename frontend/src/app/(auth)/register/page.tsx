"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { useAuth } from "@/hooks/useAuth";
import { RegisterForm } from "./RegisterForm";
import type { RegisterFieldErrors } from "./RegisterForm";

function validate(
  email: string,
  password: string,
  displayName: string,
): RegisterFieldErrors {
  const errs: RegisterFieldErrors = {};
  if (!email.includes("@")) errs.email = "Введите корректный email.";
  if (password.length < 8) errs.password = "Минимум 8 символов.";
  if (displayName.trim().length < 2)
    errs.displayName = "Минимум 2 символа.";
  return errs;
}

export default function RegisterPage() {
  const router = useRouter();
  const {
    isAuthenticated,
    isLoading: authLoading,
    register,
  } = useAuth();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [fieldErrors, setFieldErrors] = useState<RegisterFieldErrors>({});
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!authLoading && isAuthenticated) {
      router.replace("/chat");
    }
  }, [authLoading, isAuthenticated, router]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    const errs = validate(email, password, displayName);
    setFieldErrors(errs);
    if (Object.keys(errs).length > 0) return;

    setSubmitting(true);
    try {
      await register(email, password, displayName.trim());
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Не удалось зарегистрироваться.",
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <RegisterForm
      email={email}
      password={password}
      displayName={displayName}
      fieldErrors={fieldErrors}
      error={error}
      isLoading={submitting}
      onEmailChange={setEmail}
      onPasswordChange={setPassword}
      onDisplayNameChange={setDisplayName}
      onSubmit={handleSubmit}
    />
  );
}
