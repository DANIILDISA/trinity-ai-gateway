"use client";

import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { formatRelativeDate } from "@/lib/formatting";
import type {
  AlertItem,
  AlertSeverity,
  AlertStatus,
} from "@/types/admin";

export interface AlertsPageProps {
  alerts: AlertItem[];
  unreadCount: number;
  total: number;
  page: number;
  perPage: number;
  statusFilter: AlertStatus | "all";
  severityFilter: AlertSeverity | "all";
  isLoading: boolean;
  error: string | null;
  onStatusFilter: (s: AlertStatus | "all") => void;
  onSeverityFilter: (s: AlertSeverity | "all") => void;
  onPageChange: (page: number) => void;
  onAcknowledge: (id: string) => void;
  onResolve: (id: string) => void;
  onConfirmRate: (id: string) => void;
}

// Substring of the title raised by the manual-confirm exchange-rate updater
// (see backend app/tasks/exchange_rate.py). Matching it surfaces the dedicated
// "apply this CBR rate" action.
const PENDING_RATE_TITLE = "курс ЦБ к подтверждению";

const STATUS_TABS: { value: AlertStatus | "all"; label: string }[] = [
  { value: "all", label: "Все" },
  { value: "new", label: "Новые" },
  { value: "acknowledged", label: "Подтверждённые" },
  { value: "resolved", label: "Решённые" },
];

const SEVERITY_TABS: { value: AlertSeverity | "all"; label: string }[] = [
  { value: "all", label: "Все" },
  { value: "critical", label: "Критические" },
  { value: "warning", label: "Предупреждения" },
  { value: "info", label: "Инфо" },
];

export function AlertsPage({
  alerts,
  unreadCount,
  total,
  page,
  perPage,
  statusFilter,
  severityFilter,
  isLoading,
  error,
  onStatusFilter,
  onSeverityFilter,
  onPageChange,
  onAcknowledge,
  onResolve,
  onConfirmRate,
}: AlertsPageProps) {
  const totalPages = Math.max(1, Math.ceil(total / perPage));

  return (
    <div className="flex flex-col gap-4">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">
          Алерты{unreadCount > 0 ? ` (${unreadCount} новых)` : ""}
        </h1>
      </header>

      <div className="flex flex-wrap items-center gap-3">
        <FilterGroup
          label="Статус"
          options={STATUS_TABS}
          value={statusFilter}
          onChange={onStatusFilter}
        />
        <FilterGroup
          label="Важность"
          options={SEVERITY_TABS}
          value={severityFilter}
          onChange={onSeverityFilter}
        />
      </div>

      {error ? (
        <div
          role="alert"
          className="rounded border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700"
        >
          {error}
        </div>
      ) : null}

      <ul className="flex flex-col gap-2">
        {isLoading && alerts.length === 0 ? (
          Array.from({ length: 3 }).map((_, i) => (
            <li
              key={i}
              className="h-20 animate-pulse rounded border border-neutral-200 bg-neutral-100"
            />
          ))
        ) : alerts.length === 0 ? (
          <li className="rounded border border-neutral-200 bg-white px-4 py-8 text-center text-sm text-neutral-500">
            Алертов нет.
          </li>
        ) : (
          alerts.map((a) => (
            <AlertRow
              key={a.id}
              alert={a}
              onAcknowledge={() => onAcknowledge(a.id)}
              onResolve={() => onResolve(a.id)}
              onConfirmRate={() => onConfirmRate(a.id)}
            />
          ))
        )}
      </ul>

      <footer className="flex items-center justify-between text-sm">
        <span className="text-xs text-neutral-500">
          Всего: {total.toLocaleString("ru-RU")}
        </span>
        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            size="sm"
            disabled={page <= 1 || isLoading}
            onClick={() => onPageChange(page - 1)}
          >
            ←
          </Button>
          <span className="text-neutral-600">
            Страница {page} из {totalPages}
          </span>
          <Button
            variant="secondary"
            size="sm"
            disabled={page >= totalPages || isLoading}
            onClick={() => onPageChange(page + 1)}
          >
            →
          </Button>
        </div>
      </footer>
    </div>
  );
}

function FilterGroup<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-xs uppercase tracking-wide text-neutral-500">
        {label}:
      </span>
      <div className="inline-flex overflow-hidden rounded border border-neutral-300 bg-white text-xs">
        {options.map((o) => {
          const active = o.value === value;
          return (
            <button
              key={o.value}
              type="button"
              onClick={() => onChange(o.value)}
              className={`px-3 py-1 transition-colors ${
                active
                  ? "bg-neutral-900 text-white"
                  : "text-neutral-700 hover:bg-neutral-100"
              }`}
            >
              {o.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function SeverityBadge({ severity }: { severity: AlertSeverity }) {
  const map: Record<AlertSeverity, { label: string; cls: string }> = {
    critical: { label: "КРИТ", cls: "bg-rose-600 text-white" },
    warning: { label: "ПРЕД", cls: "bg-amber-500 text-white" },
    info: { label: "ИНФО", cls: "bg-blue-500 text-white" },
  };
  const entry = map[severity];
  return (
    <span
      className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-bold tracking-wide ${entry.cls}`}
    >
      {entry.label}
    </span>
  );
}

function StatusLabel({ status }: { status: AlertStatus }) {
  const map: Record<AlertStatus, string> = {
    new: "Новый",
    acknowledged: "Подтверждён",
    resolved: "Решён",
  };
  return (
    <span className="text-xs text-neutral-500">{map[status]}</span>
  );
}

function AlertRow({
  alert,
  onAcknowledge,
  onResolve,
  onConfirmRate,
}: {
  alert: AlertItem;
  onAcknowledge: () => void;
  onResolve: () => void;
  onConfirmRate: () => void;
}) {
  const unread = alert.status === "new";
  // A pending CBR-rate alert that hasn't been resolved yet can be applied
  // directly. Labelled "Подтвердить курс" to avoid colliding with the generic
  // acknowledge button ("Подтвердить") rendered for every new alert.
  const canConfirmRate =
    alert.status !== "resolved" && alert.title.includes(PENDING_RATE_TITLE);

  return (
    <li
      className={`rounded border ${
        unread ? "border-neutral-300 bg-white" : "border-neutral-200 bg-neutral-50/50"
      } p-3`}
    >
      <div className="flex flex-wrap items-start gap-3">
        <SeverityBadge severity={alert.severity} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-2">
            <h3
              className={`text-sm ${unread ? "font-semibold text-neutral-900" : "text-neutral-800"}`}
            >
              {alert.title}
            </h3>
            <span className="text-[11px] text-neutral-400">
              · {alert.source}
            </span>
            <StatusLabel status={alert.status} />
          </div>
          <p className="mt-1 text-sm text-neutral-700">{alert.message}</p>
          <div className="mt-1 text-xs text-neutral-500">
            {formatRelativeDate(alert.created_at)}
            {alert.resolved_at ? ` · решено ${formatRelativeDate(alert.resolved_at)}` : ""}
          </div>
        </div>
        <div className="flex shrink-0 gap-2">
          {canConfirmRate ? (
            <Button size="sm" onClick={onConfirmRate}>
              Подтвердить курс
            </Button>
          ) : null}
          {alert.status === "new" ? (
            <Button size="sm" variant="secondary" onClick={onAcknowledge}>
              Подтвердить
            </Button>
          ) : null}
          {alert.status !== "resolved" ? (
            <Button size="sm" onClick={onResolve}>
              Решить
            </Button>
          ) : null}
        </div>
      </div>
    </li>
  );
}

export default AlertsPage;
