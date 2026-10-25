"use client";

import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import { formatBalance } from "@/lib/formatting";
import type { ExportSummaryResponse } from "@/types/admin";

export interface ExportPageProps {
  dateFrom: string;
  dateTo: string;
  isExportingTx: boolean;
  isExportingPerUser: boolean;
  isSummaryLoading: boolean;
  summary: ExportSummaryResponse | null;
  error: string | null;

  onDateFromChange: (value: string) => void;
  onDateToChange: (value: string) => void;
  onExportTransactions: () => void;
  onExportSummary: () => void;
  onExportPerUser: () => void;
}

function marginColor(p: number): string {
  if (p > 15) return "text-emerald-700";
  if (p >= 5) return "text-amber-600";
  return "text-rose-700";
}

export function ExportPage({
  dateFrom,
  dateTo,
  isExportingTx,
  isExportingPerUser,
  isSummaryLoading,
  summary,
  error,
  onDateFromChange,
  onDateToChange,
  onExportTransactions,
  onExportSummary,
  onExportPerUser,
}: ExportPageProps) {
  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-semibold tracking-tight">Экспорт</h1>

      <section className="rounded-lg border border-neutral-200 bg-white p-6">
        <h2 className="text-lg font-semibold tracking-tight">Период</h2>
        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 sm:max-w-md">
          <Input
            label="Дата от"
            type="date"
            value={dateFrom}
            onChange={(e) => onDateFromChange(e.target.value)}
          />
          <Input
            label="Дата до"
            type="date"
            value={dateTo}
            onChange={(e) => onDateToChange(e.target.value)}
          />
        </div>

        <div className="mt-4 flex flex-wrap gap-2">
          <Button
            onClick={onExportTransactions}
            loading={isExportingTx}
            disabled={isExportingTx}
          >
            Транзакции (CSV)
          </Button>
          <Button
            variant="secondary"
            onClick={onExportSummary}
            loading={isSummaryLoading}
            disabled={isSummaryLoading}
          >
            Сводка за период
          </Button>
          <Button
            variant="secondary"
            onClick={onExportPerUser}
            loading={isExportingPerUser}
            disabled={isExportingPerUser}
          >
            По пользователям (CSV)
          </Button>
        </div>

        {error ? (
          <p role="alert" className="mt-3 text-sm text-rose-600">
            {error}
          </p>
        ) : null}
      </section>

      {isSummaryLoading && !summary ? (
        <div className="flex justify-center py-8">
          <Spinner size="lg" />
        </div>
      ) : summary ? (
        <SummaryCard summary={summary} />
      ) : null}
    </div>
  );
}

function SummaryCard({ summary }: { summary: ExportSummaryResponse }) {
  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-6">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-lg font-semibold tracking-tight">Сводка</h2>
        <span className="text-xs text-neutral-500">
          {summary.period.date_from} — {summary.period.date_to}
        </span>
      </header>

      <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2">
        <Block label="Выручка">
          <p className="text-2xl font-semibold">
            {formatBalance(summary.revenue.total_display)}
          </p>
          <p className="text-xs text-neutral-500">
            {summary.revenue.credit_count} пополнений ·{" "}
            {summary.revenue.debit_count} списаний
          </p>
        </Block>

        <Block label="Затраты OpenRouter">
          <p className="text-2xl font-semibold">${summary.costs.total_usd_display}</p>
        </Block>

        <Block label="Маржа">
          <p
            className={`text-2xl font-semibold ${marginColor(summary.margin.percent)}`}
          >
            {summary.margin.percent.toFixed(1)}%
          </p>
          <p className="text-xs text-neutral-500">
            Эффективная выручка (за вычетом overdraft):{" "}
            {formatBalance(
              (summary.margin.effective_revenue_micro_rub / 1_000_000).toFixed(4),
            )}
          </p>
        </Block>

        <Block label="Пользователи">
          <p className="text-sm">
            <span className="font-medium">{summary.users.unique_payers}</span>{" "}
            уникальных плательщиков
          </p>
          <p className="text-sm">
            <span className="font-medium">{summary.users.new_registrations}</span>{" "}
            новых регистраций
          </p>
        </Block>

        <Block label="Запросы" wide>
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
            <span>
              Всего:{" "}
              <span className="font-medium">{summary.requests.total}</span>
            </span>
            <span>
              Завершено:{" "}
              <span className="font-medium text-emerald-700">
                {summary.requests.completed}
              </span>
            </span>
            <span>
              Частично:{" "}
              <span className="font-medium text-amber-700">
                {summary.requests.partial}
              </span>
            </span>
            <span>
              Ошибки:{" "}
              <span className="font-medium text-rose-700">
                {summary.requests.failed}
              </span>
            </span>
          </div>
        </Block>
      </div>
    </section>
  );
}

function Block({
  label,
  children,
  wide = false,
}: {
  label: string;
  children: React.ReactNode;
  wide?: boolean;
}) {
  return (
    <div
      className={`rounded border border-neutral-100 bg-neutral-50 p-4 ${
        wide ? "md:col-span-2" : ""
      }`}
    >
      <p className="text-xs uppercase tracking-wide text-neutral-500">{label}</p>
      <div className="mt-1">{children}</div>
    </div>
  );
}

export default ExportPage;
