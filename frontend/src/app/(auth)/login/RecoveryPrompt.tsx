"use client";

import { Button } from "@/components/ui/Button";

export interface RecoveryPromptProps {
  email: string;
  error: string | null;
  isLoading: boolean;
  onRecover: () => void;
  onCancel: () => void;
}

export function RecoveryPrompt({
  email,
  error,
  isLoading,
  onRecover,
  onCancel,
}: RecoveryPromptProps) {
  return (
    <section className="flex flex-col gap-6 rounded-lg border border-neutral-800 bg-neutral-900 p-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight text-neutral-100">
          Восстановить аккаунт?
        </h1>
        <p className="text-sm text-neutral-400">
          Аккаунт <span className="font-mono text-neutral-200">{email}</span>{" "}
          помечен на удаление. Восстановите доступ, чтобы продолжить работу.
        </p>
      </header>

      {error ? (
        <p role="alert" className="text-sm text-red-400">
          {error}
        </p>
      ) : null}

      <div className="flex flex-col gap-2">
        <Button onClick={onRecover} loading={isLoading} disabled={isLoading}>
          Восстановить аккаунт
        </Button>
        <Button variant="secondary" onClick={onCancel} disabled={isLoading}>
          Отмена
        </Button>
      </div>
    </section>
  );
}

export default RecoveryPrompt;
