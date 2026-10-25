"use client";

import Link from "next/link";

import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";

export interface RegisterFieldErrors {
  email?: string;
  password?: string;
  displayName?: string;
}

export interface RegisterFormProps {
  email: string;
  password: string;
  displayName: string;
  fieldErrors: RegisterFieldErrors;
  error: string | null;
  isLoading: boolean;
  onEmailChange: (value: string) => void;
  onPasswordChange: (value: string) => void;
  onDisplayNameChange: (value: string) => void;
  onSubmit: (e: React.FormEvent) => void;
}

export function RegisterForm({
  email,
  password,
  displayName,
  fieldErrors,
  error,
  isLoading,
  onEmailChange,
  onPasswordChange,
  onDisplayNameChange,
  onSubmit,
}: RegisterFormProps) {
  return (
    <section className="flex flex-col gap-6 rounded-lg border border-neutral-800 bg-neutral-900 p-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight text-neutral-100">
          Создание аккаунта
        </h1>
        <p className="text-sm text-neutral-400">
          Зарегистрируйтесь, чтобы начать пользоваться Trinity.
        </p>
      </header>

      <form className="flex flex-col gap-4" onSubmit={onSubmit} noValidate>
        <Input
          label="Email"
          type="email"
          autoComplete="email"
          inputMode="email"
          required
          value={email}
          onChange={(e) => onEmailChange(e.target.value)}
          error={fieldErrors.email}
        />
        <Input
          label="Пароль"
          type="password"
          autoComplete="new-password"
          required
          value={password}
          onChange={(e) => onPasswordChange(e.target.value)}
          error={fieldErrors.password}
        />
        <p className="-mt-2 text-xs text-neutral-500">Минимум 8 символов.</p>

        <Input
          label="Как вас зовут?"
          type="text"
          autoComplete="name"
          required
          value={displayName}
          onChange={(e) => onDisplayNameChange(e.target.value)}
          error={fieldErrors.displayName}
        />

        {error ? (
          <p role="alert" className="text-sm text-red-400">
            {error}
          </p>
        ) : null}

        <Button type="submit" loading={isLoading} disabled={isLoading}>
          Создать аккаунт
        </Button>

        <p className="text-xs leading-relaxed text-neutral-500">
          Регистрируясь, вы соглашаетесь с{" "}
          <Link
            href="/legal/terms"
            className="text-sky-400 hover:underline"
          >
            Условиями использования
          </Link>{" "}
          и{" "}
          <Link
            href="/legal/privacy"
            className="text-sky-400 hover:underline"
          >
            Политикой конфиденциальности
          </Link>
          .
        </p>
      </form>

      <footer className="text-sm text-neutral-400">
        <Link className="text-sky-400 hover:underline" href="/login">
          Уже есть аккаунт? Войти
        </Link>
      </footer>
    </section>
  );
}

export default RegisterForm;
