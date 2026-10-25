"use client";

import { useState } from "react";

import { formatBalance, formatTokenCount } from "@/lib/formatting";
import type {
  DashboardPeriod,
  DashboardResponse,
  RevenueByModelResponse,
} from "@/types/admin";

export interface AdminDashboardProps {
  data: DashboardResponse | null;
  revenueByModel: RevenueByModelResponse | null;
  isLoading: boolean;
  isLoadingModels: boolean;
  error: string | null;
  modelsError: string | null;
  period: DashboardPeriod;
  onPeriodChange: (period: DashboardPeriod) => void;
  onSaveBalance: (balanceUsd: number) => Promise<void>;
}

const PERIOD_LABEL: Record<DashboardPeriod, string> = {
  today: "Сегодня",
  week: "Неделя",
  month: "Месяц",
};

function revenueForPeriod(
  data: DashboardResponse,
  period: DashboardPeriod,
): string {
  switch (period) {
    case "today":
      return formatBalance(data.revenue.today_display);
    case "week":
      return formatBalance(data.revenue.week_display);
    case "month":
      return formatBalance(data.revenue.month_display);
  }
}

function marginForPeriod(
  data: DashboardResponse,
  period: DashboardPeriod,
): number {
  switch (period) {
    case "today":
      return data.margin.today_percent;
    case "week":
      return data.margin.week_percent;
    case "month":
      return data.margin.month_percent;
  }
}

function overdraftForPeriod(
  data: DashboardResponse,
  period: DashboardPeriod,
): string {
  switch (period) {
    case "today":
      return formatBalance(data.overdraft.today_display);
    case "week":
      return formatBalance(data.overdraft.week_display);
    case "month":
      return formatBalance(data.overdraft.month_display);
  }
}

function costForPeriod(
  data: DashboardResponse,
  period: DashboardPeriod,
): number {
  switch (period) {
    case "today":
      return data.costs.today_usd;
    case "week":
      return data.costs.week_usd;
    case "month":
      return data.costs.month_usd;
  }
}

function marginColor(percent: number): string {
  if (percent > 15) return "text-emerald-700";
  if (percent >= 5) return "text-amber-600";
  return "text-rose-700";
}

export function AdminDashboard({
  data,
  revenueByModel,
  isLoading,
  isLoadingModels,
  error,
  modelsError,
  period,
  onPeriodChange,
  onSaveBalance,
}: AdminDashboardProps) {
  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Дашборд</h1>
          <p className="text-sm text-neutral-600">
            Финансовые и операционные метрики (обновление каждые 30 секунд).
          </p>
        </div>
        <PeriodSelector period={period} onPeriodChange={onPeriodChange} />
      </header>

      {error ? (
        <div
          role="alert"
          className="rounded border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700"
        >
          {data
            ? `${error} Показаны последние известные данные.`
            : "Не удалось загрузить данные дашборда."}
        </div>
      ) : null}

      <section className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <MetricCard
          label={`Выручка (${PERIOD_LABEL[period].toLowerCase()})`}
          value={data ? revenueForPeriod(data, period) : null}
          isLoading={isLoading && !data}
        />
        <MetricCard
          label={`Маржа (${PERIOD_LABEL[period].toLowerCase()})`}
          value={
            data ? `${marginForPeriod(data, period).toFixed(1)}%` : null
          }
          valueClass={data ? marginColor(marginForPeriod(data, period)) : ""}
          isLoading={isLoading && !data}
        />
        <MetricCard
          label={`Поглощено овердрафта (${PERIOD_LABEL[period].toLowerCase()})`}
          value={data ? overdraftForPeriod(data, period) : null}
          isLoading={isLoading && !data}
        />
        <MetricCard
          label="Запросов сегодня"
          value={data ? data.total_requests_today.toLocaleString("ru-RU") : null}
          isLoading={isLoading && !data}
        />
        <MetricCard
          label="Активных пользователей"
          value={data ? data.active_users_today.toLocaleString("ru-RU") : null}
          isLoading={isLoading && !data}
        />
      </section>

      <section className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <OpenRouterBalanceCard
          data={data}
          isLoading={isLoading && !data}
          onSaveBalance={onSaveBalance}
        />
        <StatusCard
          label="Сверка"
          value={
            data
              ? data.reconciliation.status === "ok"
                ? "✓ OK"
                : `✗ Расхождение ${data.reconciliation.discrepancy_percent.toFixed(2)}%`
              : null
          }
          tone={
            data && data.reconciliation.status === "ok"
              ? "ok"
              : "warning"
          }
          isLoading={isLoading && !data}
        />
      </section>

      <RevenueByModelTable
        data={revenueByModel}
        isLoading={isLoadingModels}
        error={modelsError}
        period={period}
      />
    </div>
  );
}

function PeriodSelector({
  period,
  onPeriodChange,
}: {
  period: DashboardPeriod;
  onPeriodChange: (p: DashboardPeriod) => void;
}) {
  const periods: DashboardPeriod[] = ["today", "week", "month"];
  return (
    <div
      role="tablist"
      aria-label="Период"
      className="inline-flex overflow-hidden rounded border border-neutral-300 bg-white text-sm"
    >
      {periods.map((p) => {
        const active = p === period;
        return (
          <button
            key={p}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onPeriodChange(p)}
            className={`px-3 py-1.5 transition-colors ${
              active
                ? "bg-neutral-900 text-white"
                : "text-neutral-700 hover:bg-neutral-100"
            }`}
          >
            {PERIOD_LABEL[p]}
          </button>
        );
      })}
    </div>
  );
}

function MetricCard({
  label,
  value,
  valueClass = "",
  isLoading,
}: {
  label: string;
  value: string | null;
  valueClass?: string;
  isLoading: boolean;
}) {
  return (
    <div className="rounded-lg border border-neutral-200 bg-white p-5">
      <p className="text-xs uppercase tracking-wide text-neutral-500">
        {label}
      </p>
      <div className={`mt-2 text-3xl font-semibold tracking-tight ${valueClass}`}>
        {isLoading ? (
          <span className="inline-block h-9 w-32 animate-pulse rounded bg-neutral-200" />
        ) : (
          (value ?? "—")
        )}
      </div>
    </div>
  );
}

function StatusCard({
  label,
  value,
  tone,
  isLoading,
}: {
  label: string;
  value: string | null;
  tone: "ok" | "warning";
  isLoading: boolean;
}) {
  const color =
    tone === "warning" ? "text-rose-700" : "text-emerald-700";
  return (
    <div className="rounded-lg border border-neutral-200 bg-white p-5">
      <p className="text-xs uppercase tracking-wide text-neutral-500">
        {label}
      </p>
      <div className={`mt-2 text-2xl font-semibold tracking-tight ${color}`}>
        {isLoading ? (
          <span className="inline-block h-7 w-24 animate-pulse rounded bg-neutral-200" />
        ) : (
          (value ?? "—")
        )}
      </div>
    </div>
  );
}

const SOURCE_LABEL: Record<
  DashboardResponse["openrouter_balance_source"],
  string
> = {
  live: "",
  manual: "(вручную)",
  depleted: "(кредиты исчерпаны)",
  unavailable: "(недоступно)",
};

function OpenRouterBalanceCard({
  data,
  isLoading,
  onSaveBalance,
}: {
  data: DashboardResponse | null;
  isLoading: boolean;
  onSaveBalance: (balanceUsd: number) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [input, setInput] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const balance = data?.openrouter_balance_usd ?? null;
  const source = data?.openrouter_balance_source ?? "unavailable";
  const tone = data?.openrouter_balance_warning ? "warning" : "ok";
  const color = tone === "warning" ? "text-rose-700" : "text-emerald-700";

  const startEdit = () => {
    setError(null);
    setInput(balance !== null ? balance.toFixed(2) : "");
    setEditing(true);
  };

  const handleSave = async () => {
    const value = Number(input);
    if (!Number.isFinite(value) || value < 0) {
      setError("Введите неотрицательное число.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSaveBalance(value);
      setEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сохранить.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded-lg border border-neutral-200 bg-white p-5">
      <div className="flex items-center justify-between">
        <p className="text-xs uppercase tracking-wide text-neutral-500">
          Баланс OpenRouter{" "}
          {source !== "live" && SOURCE_LABEL[source] ? (
            <span className="normal-case text-neutral-400">
              {SOURCE_LABEL[source]}
            </span>
          ) : null}
        </p>
        {data && !editing ? (
          <button
            type="button"
            aria-label="Изменить баланс"
            onClick={startEdit}
            className="text-neutral-400 transition-colors hover:text-neutral-700"
          >
            ✏️
          </button>
        ) : null}
      </div>

      {editing ? (
        <div className="mt-2 flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <span className="text-neutral-500">$</span>
            <input
              type="number"
              step="0.01"
              min="0"
              autoFocus
              value={input}
              onChange={(e) => setInput(e.target.value)}
              className="w-32 rounded border border-neutral-300 px-2 py-1 text-lg tabular-nums"
            />
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => void handleSave()}
              disabled={saving}
              className="rounded bg-neutral-900 px-3 py-1 text-sm text-white disabled:opacity-50"
            >
              Сохранить
            </button>
            <button
              type="button"
              onClick={() => setEditing(false)}
              disabled={saving}
              className="rounded border border-neutral-300 px-3 py-1 text-sm text-neutral-700"
            >
              Отмена
            </button>
          </div>
          {error ? <p className="text-xs text-rose-600">{error}</p> : null}
        </div>
      ) : (
        <div className={`mt-2 text-2xl font-semibold tracking-tight ${color}`}>
          {isLoading ? (
            <span className="inline-block h-7 w-24 animate-pulse rounded bg-neutral-200" />
          ) : data ? (
            source === "unavailable" ? (
              "Недоступно"
            ) : (
              `$${(balance ?? 0).toFixed(2)}`
            )
          ) : (
            "—"
          )}
        </div>
      )}
    </div>
  );
}

function RevenueByModelTable({
  data,
  isLoading,
  error,
  period,
}: {
  data: RevenueByModelResponse | null;
  isLoading: boolean;
  error: string | null;
  period: DashboardPeriod;
}) {
  return (
    <section className="rounded-lg border border-neutral-200 bg-white">
      <header className="border-b border-neutral-200 px-4 py-3">
        <h2 className="text-base font-semibold tracking-tight">
          Выручка по моделям ({PERIOD_LABEL[period].toLowerCase()})
        </h2>
      </header>

      {error ? (
        <div className="px-4 py-3 text-sm text-rose-700">{error}</div>
      ) : null}

      {isLoading && !data ? (
        <ul className="flex flex-col gap-2 p-4">
          {Array.from({ length: 3 }).map((_, i) => (
            <li
              key={i}
              className="h-10 animate-pulse rounded bg-neutral-100"
            />
          ))}
        </ul>
      ) : data && data.models.length === 0 ? (
        <p className="px-4 py-6 text-sm text-neutral-500">
          Нет данных за период.
        </p>
      ) : data ? (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-neutral-50 text-left text-xs uppercase tracking-wide text-neutral-500">
              <tr>
                <th className="px-4 py-2 font-medium">Модель</th>
                <th className="px-4 py-2 text-right font-medium">Запросов</th>
                <th className="px-4 py-2 text-right font-medium">Токены</th>
                <th className="px-4 py-2 text-right font-medium">Выручка</th>
                <th className="px-4 py-2 text-right font-medium">Стоимость</th>
                <th className="px-4 py-2 text-right font-medium">Маржа</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-100">
              {data.models.map((m) => (
                <tr key={m.model_id}>
                  <td className="px-4 py-2">
                    <div className="font-medium text-neutral-900">
                      {m.model_name}
                    </div>
                    <div className="text-xs text-neutral-500">{m.model_id}</div>
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {formatTokenCount(m.request_count)}
                  </td>
                  <td className="px-4 py-2 text-right text-xs text-neutral-600 tabular-nums">
                    {formatTokenCount(m.total_input_tokens)} вх
                    <br />
                    {formatTokenCount(m.total_output_tokens)} исх
                  </td>
                  <td className="px-4 py-2 text-right font-mono tabular-nums">
                    {formatBalance(m.revenue_display)}
                  </td>
                  <td className="px-4 py-2 text-right font-mono tabular-nums">
                    ${m.cost_usd.toFixed(4)}
                  </td>
                  <td
                    className={`px-4 py-2 text-right font-mono tabular-nums ${marginColor(m.margin_percent)}`}
                  >
                    {m.margin_percent.toFixed(1)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

export default AdminDashboard;
