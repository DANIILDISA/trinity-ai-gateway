"use client";

import Link from "next/link";

import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";

export type ResetStep = "request" | "confirm";

export interface ResetPasswordFormProps {
  step: ResetStep;
  email: string;
  newPassword: string;
  error: string | null;
  success: string | null;
  isLoading: boolean;
  onEmailChange: (value: string) => void;
  onPasswordChange: (value: string) => void;
  onSubmit: (e: React.FormEvent) => void;
}

export function ResetPasswordForm({
  step,
  email,
  newPassword,
  error,
  success,
  isLoading,
  onEmailChange,
  onPasswordChange,
  onSubmit,
}: ResetPasswordFormProps) {
  const title =
    step === "request" ? "Сброс пароля" : "Установите новый пароль";
  const description =
    step === "request"
      ? "Мы отправим ссылку для восстановления на ваш email."
      : "Придумайте новый пароль для вашего аккаунта.";

  return (
    <section className="flex flex-col gap-6 rounded-lg border border-neutral-800 bg-neutral-900 p-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight text-neutral-100">
          {title}
        </h1>
        <p className="text-sm text-neutral-400">{description}</p>
      </header>

      <form className="flex flex-col gap-4" onSubmit={onSubmit} noValidate>
        {step === "request" ? (
          <Input
            label="Email"
            type="email"
            autoComplete="email"
            inputMode="email"
            required
            value={email}
            onChange={(e) => onEmailChange(e.target.value)}
          />
        ) : (
          <>
            <Input
              label="Новый пароль"
              type="password"
              autoComplete="new-password"
              required
              value={newPassword}
              onChange={(e) => onPasswordChange(e.target.value)}
            />
            <p className="-mt-2 text-xs text-neutral-500">
              Минимум 8 символов.
            </p>
          </>
        )}

        {error ? (
          <p role="alert" className="text-sm text-red-400">
            {error}
          </p>
        ) : null}

        {success ? (
          <p
            role="status"
            className="rounded border border-emerald-800/50 bg-emerald-950/40 px-3 py-2 text-sm text-emerald-300"
          >
            {success}
          </p>
        ) : null}

        <Button type="submit" loading={isLoading} disabled={isLoading}>
          {step === "request" ? "Отправить ссылку" : "Установить пароль"}
        </Button>
      </form>

      <footer className="text-sm text-neutral-400">
        <Link className="text-sky-400 hover:underline" href="/login">
          Вернуться ко входу
        </Link>
      </footer>
    </section>
  );
}

export default ResetPasswordForm;
