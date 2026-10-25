"use client";

import Link from "next/link";

import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";

export interface LoginFormProps {
  email: string;
  password: string;
  error: string | null;
  isLoading: boolean;
  onEmailChange: (value: string) => void;
  onPasswordChange: (value: string) => void;
  onSubmit: (e: React.FormEvent) => void;
  onYandexLogin: () => void;
}

export function LoginForm({
  email,
  password,
  error,
  isLoading,
  onEmailChange,
  onPasswordChange,
  onSubmit,
  onYandexLogin,
}: LoginFormProps) {
  return (
    <section className="flex flex-col gap-6 rounded-lg border border-neutral-800 bg-neutral-900 p-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight text-neutral-100">
          Вход
        </h1>
        <p className="text-sm text-neutral-400">
          Войдите, чтобы продолжить работу.
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
        />
        <Input
          label="Пароль"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => onPasswordChange(e.target.value)}
        />

        {error ? (
          <p role="alert" className="text-sm text-red-400">
            {error}
          </p>
        ) : null}

        <Button type="submit" loading={isLoading} disabled={isLoading}>
          Войти
        </Button>
      </form>

      <div className="flex items-center gap-3 text-xs uppercase tracking-wide text-neutral-500">
        <span className="h-px flex-1 bg-neutral-800" />
        или
        <span className="h-px flex-1 bg-neutral-800" />
      </div>

      <Button variant="secondary" onClick={onYandexLogin} disabled={isLoading}>
        Войти через Яндекс
      </Button>

      <footer className="flex flex-col gap-2 text-sm text-neutral-400">
        <Link className="text-sky-400 hover:underline" href="/register">
          Нет аккаунта? Зарегистрироваться
        </Link>
        <Link className="text-sky-400 hover:underline" href="/reset-password">
          Забыли пароль?
        </Link>
      </footer>
    </section>
  );
}

export default LoginForm;
