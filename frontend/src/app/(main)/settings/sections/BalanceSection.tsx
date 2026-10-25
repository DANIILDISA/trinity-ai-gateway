"use client";

import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import { formatDateTime, formatTransactionAmount } from "@/lib/formatting";
import type { TransactionItem } from "@/types/api";
import {
  AccentButton,
  EmptyState,
  Panel,
  SectionShell,
} from "./primitives";

export interface BalanceSectionProps {
  balance: string | null;
  isLoadingBalance: boolean;
  amount: string;
  amountError: string | null;
  isSubmittingTopUp: boolean;
  transactions: TransactionItem[];
  isLoadingTransactions: boolean;
  hasMoreTransactions: boolean;
  pendingPaymentNotice: string | null;
  onAmountChange: (value: string) => void;
  onTopUp: () => void;
  onLoadMoreTransactions: () => void;
}

export function BalanceSection(props: BalanceSectionProps) {
  return (
    <SectionShell title="Баланс" description="Текущий баланс и пополнение через СБП.">
      {props.pendingPaymentNotice ? (
        <div
          role="status"
          className="rounded-lg border border-amber-800/50 bg-amber-950/40 px-4 py-3 text-sm text-amber-200"
        >
          {props.pendingPaymentNotice}
        </div>
      ) : null}

      <Panel
        title="Текущий баланс"
        action={
          <span className="font-mono text-[11px] uppercase tracking-wider text-neutral-600">
            ₽ · RUB
          </span>
        }
      >
        <div className="font-mono text-5xl font-semibold leading-none tabular-nums tracking-tight text-emerald-400">
          {props.isLoadingBalance && props.balance === null ? (
            <span className="inline-block h-12 w-48 animate-pulse rounded bg-neutral-800" />
          ) : (
            (props.balance ?? "—")
          )}
        </div>

        <form
          className="mt-6 flex flex-col gap-3 sm:max-w-md"
          onSubmit={(e) => {
            e.preventDefault();
            props.onTopUp();
          }}
          noValidate
        >
          <Input
            label="Сумма пополнения (₽)"
            type="number"
            inputMode="numeric"
            min={500}
            step={100}
            placeholder="500"
            value={props.amount}
            onChange={(e) => props.onAmountChange(e.target.value)}
            error={props.amountError ?? undefined}
            disabled={props.isSubmittingTopUp}
          />
          <div>
            <AccentButton
              type="submit"
              loading={props.isSubmittingTopUp}
              disabled={props.isSubmittingTopUp}
            >
              Пополнить через СБП
            </AccentButton>
          </div>
        </form>
      </Panel>

      <Panel
        title="История операций"
        action={
          <span className="font-mono text-[11px] uppercase tracking-wider text-neutral-600">
            {props.transactions.length > 0
              ? `${props.transactions.length} зап.`
              : ""}
          </span>
        }
      >
        {props.transactions.length === 0 && !props.isLoadingTransactions ? (
          <EmptyState>Операций пока нет</EmptyState>
        ) : (
          <>
            <div className="grid grid-cols-[7rem,1fr,9rem] items-center gap-4 border-b border-neutral-800 pb-2 font-mono text-[10px] uppercase tracking-wider text-neutral-600">
              <span>Сумма</span>
              <span>Операция</span>
              <span className="text-right">Дата</span>
            </div>
            <ul className="divide-y divide-neutral-800/60">
              {props.transactions.map((tx) => (
                <TransactionRow key={tx.id} tx={tx} />
              ))}
            </ul>
          </>
        )}

        {props.isLoadingTransactions ? (
          <div className="flex justify-center py-4">
            <Spinner />
          </div>
        ) : null}

        {props.hasMoreTransactions && !props.isLoadingTransactions ? (
          <div className="mt-5 flex justify-center">
            <Button
              variant="secondary"
              size="sm"
              onClick={props.onLoadMoreTransactions}
            >
              Загрузить ещё
            </Button>
          </div>
        ) : null}
      </Panel>
    </SectionShell>
  );
}

function TransactionRow({ tx }: { tx: TransactionItem }) {
  let color: string;
  if (tx.type === "credit" || tx.type === "refund") {
    color = "text-emerald-400";
  } else if (tx.type === "debit") {
    color = "text-neutral-200";
  } else {
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
