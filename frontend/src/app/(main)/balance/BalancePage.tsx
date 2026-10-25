"use client";

import type { ButtonHTMLAttributes } from "react";

import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import { formatDateTime, formatTransactionAmount } from "@/lib/formatting";
import type { TransactionItem } from "@/types/api";

export interface BalancePageProps {
  balance: string | null;
  isLoadingBalance: boolean;
  amount: string;
  amountError: string | null;
  isSubmitting: boolean;
  transactions: TransactionItem[];
  isLoadingTx: boolean;
  hasMore: boolean;
  pendingPaymentNotice: string | null;
  onAmountChange: (value: string) => void;
  onTopUp: (e: React.FormEvent) => void;
  onLoadMore: () => void;
}

type PrimaryButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  loading?: boolean;
};

function PrimaryButton({
  loading = false,
  disabled,
  className = "",
  type = "button",
  children,
  ...rest
}: PrimaryButtonProps) {
  const isDisabled = disabled || loading;
  return (
    <button
      type={type}
      disabled={isDisabled}
      className={`inline-flex h-10 items-center justify-center gap-2 rounded-md bg-neutral-100 px-5 text-sm font-medium text-neutral-900 transition-colors hover:bg-white disabled:cursor-not-allowed disabled:bg-neutral-300 disabled:text-neutral-500 ${className}`}
      {...rest}
    >
      {loading ? <Spinner size="sm" /> : null}
      {children}
    </button>
  );
}

export function BalancePage({
  balance,
  isLoadingBalance,
  amount,
  amountError,
  isSubmitting,
  transactions,
  isLoadingTx,
  hasMore,
  pendingPaymentNotice,
  onAmountChange,
  onTopUp,
  onLoadMore,
}: BalancePageProps) {
  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-5 px-4 py-10">
      <header className="flex flex-col gap-1 border-b border-neutral-800 pb-4">
        <span className="font-mono text-[11px] uppercase tracking-[0.25em] text-neutral-500">
          // wallet
        </span>
        <h1 className="text-3xl font-semibold tracking-tight text-neutral-100">
          Баланс
        </h1>
      </header>

      {pendingPaymentNotice ? (
        <div
          role="status"
          className="rounded-lg border border-amber-800/50 bg-amber-950/40 px-4 py-3 text-sm text-amber-200"
        >
          {pendingPaymentNotice}
        </div>
      ) : null}

      <section className="overflow-hidden rounded-lg border border-neutral-800 bg-neutral-900">
        <header className="flex items-center justify-between border-b border-neutral-800 px-6 py-3">
          <span className="font-mono text-[11px] uppercase tracking-wider text-neutral-500">
            Текущий баланс
          </span>
          <span className="font-mono text-[11px] uppercase tracking-wider text-neutral-600">
            ₽ · RUB
          </span>
        </header>

        <div className="px-6 py-7">
          <div className="font-mono text-6xl font-semibold leading-none tabular-nums tracking-tight text-neutral-100">
            {isLoadingBalance && balance === null ? (
              <span className="inline-block h-14 w-56 animate-pulse rounded bg-neutral-800" />
            ) : (
              (balance ?? "—")
            )}
          </div>

          <form
            className="mt-7 flex flex-col gap-3 sm:max-w-md"
            onSubmit={onTopUp}
            noValidate
          >
            <Input
              label="Сумма пополнения (₽)"
              type="number"
              inputMode="numeric"
              min={500}
              step={100}
              placeholder="500"
              value={amount}
              onChange={(e) => onAmountChange(e.target.value)}
              error={amountError ?? undefined}
              disabled={isSubmitting}
            />
            <div>
              <PrimaryButton
                type="submit"
                loading={isSubmitting}
                disabled={isSubmitting}
              >
                Пополнить через СБП
              </PrimaryButton>
            </div>
          </form>
        </div>
      </section>

      <section className="overflow-hidden rounded-lg border border-neutral-800 bg-neutral-900">
        <header className="flex items-center justify-between border-b border-neutral-800 px-6 py-3">
          <span className="font-mono text-[11px] uppercase tracking-wider text-neutral-500">
            История операций
          </span>
          <span className="font-mono text-[11px] uppercase tracking-wider text-neutral-600">
            {transactions.length > 0 ? `${transactions.length} зап.` : ""}
          </span>
        </header>

        <div className="px-6 py-5">
          {transactions.length === 0 && !isLoadingTx ? (
            <p className="py-6 font-mono text-xs uppercase tracking-wider text-neutral-600">
              Операций пока нет
            </p>
          ) : (
            <>
              <div className="grid grid-cols-[7rem,1fr,9rem] items-center gap-4 border-b border-neutral-800 pb-2 font-mono text-[10px] uppercase tracking-wider text-neutral-600">
                <span>Сумма</span>
                <span>Операция</span>
                <span className="text-right">Дата</span>
              </div>
              <ul className="divide-y divide-neutral-800/60">
                {transactions.map((tx) => (
                  <TransactionRow key={tx.id} tx={tx} />
                ))}
              </ul>
            </>
          )}

          {isLoadingTx ? (
            <div className="flex justify-center py-4">
              <Spinner />
            </div>
          ) : null}

          {hasMore && !isLoadingTx ? (
            <div className="mt-5 flex justify-center">
              <Button variant="secondary" size="sm" onClick={onLoadMore}>
                Загрузить ещё
              </Button>
            </div>
          ) : null}
        </div>
      </section>
    </div>
  );
}

function TransactionRow({ tx }: { tx: TransactionItem }) {
  // BUG 10: spending is neutral (no sign); top-ups/refunds are green;
  // adjustments are colored by direction.
  let color: string;
  if (tx.type === "credit" || tx.type === "refund") {
    color = "text-emerald-400";
  } else if (tx.type === "debit") {
    color = "text-neutral-200";
  } else {
    // adjustment
    color = tx.amount.trim().startsWith("-")
      ? "text-red-400"
      : "text-emerald-400";
  }

  return (
    <li className="grid grid-cols-[7rem,1fr,9rem] items-center gap-4 py-3">
      <span className={`font-mono text-sm font-medium tabular-nums ${color}`}>
        {formatTransactionAmount(tx.amount, tx.type)}
      </span>
      <span className="truncate text-sm text-neutral-300">
        {tx.description || "—"}
      </span>
      <time
        dateTime={tx.created_at}
        className="text-right font-mono text-[11px] tabular-nums text-neutral-500"
      >
        {formatDateTime(tx.created_at)}
      </time>
    </li>
  );
}

export default BalancePage;
