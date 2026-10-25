"use client";

import { useState } from "react";
import Link from "next/link";

import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import { formatDateTime } from "@/lib/formatting";
import type { AuditLogItem } from "@/types/admin";

export interface AuditFilters {
  action: string;
  actor_email: string;
  date_from: string;
  date_to: string;
}

export interface AuditPageProps {
  items: AuditLogItem[];
  isLoading: boolean;
  error: string | null;
  total: number;
  page: number;
  perPage: number;
  filters: AuditFilters;
  onFilterChange: (filters: AuditFilters) => void;
  onPageChange: (page: number) => void;
}

export function AuditPage({
  items,
  isLoading,
  error,
  total,
  page,
  perPage,
  filters,
  onFilterChange,
  onPageChange,
}: AuditPageProps) {
  const totalPages = Math.max(1, Math.ceil(total / perPage));

  const setField = (k: keyof AuditFilters, v: string) =>
    onFilterChange({ ...filters, [k]: v });

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl font-semibold tracking-tight">Аудит</h1>

      <section className="grid grid-cols-1 gap-3 rounded-lg border border-neutral-200 bg-white p-4 md:grid-cols-4">
        <Input
          label="Действие (поиск по подстроке)"
          value={filters.action}
          onChange={(e) => setField("action", e.target.value)}
        />
        <Input
          label="Email исполнителя"
          value={filters.actor_email}
          onChange={(e) => setField("actor_email", e.target.value)}
        />
        <Input
          label="Дата от"
          type="date"
          value={filters.date_from}
          onChange={(e) => setField("date_from", e.target.value)}
        />
        <Input
          label="Дата до"
          type="date"
          value={filters.date_to}
          onChange={(e) => setField("date_to", e.target.value)}
        />
      </section>

      {error ? (
        <div
          role="alert"
          className="rounded border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700"
        >
          {error}
        </div>
      ) : null}

      <div className="overflow-x-auto rounded-lg border border-neutral-200 bg-white">
        <table className="w-full text-sm">
          <thead className="bg-neutral-50 text-left text-xs uppercase tracking-wide text-neutral-500">
            <tr>
              <th className="px-4 py-2 font-medium">Время</th>
              <th className="px-4 py-2 font-medium">Действие</th>
              <th className="px-4 py-2 font-medium">Исполнитель</th>
              <th className="px-4 py-2 font-medium">Цель</th>
              <th className="px-4 py-2 font-medium">Детали</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-100">
            {isLoading && items.length === 0 ? (
              Array.from({ length: 5 }).map((_, i) => (
                <tr key={`s-${i}`}>
                  {Array.from({ length: 5 }).map((__, j) => (
                    <td key={j} className="px-4 py-3">
                      <span className="inline-block h-4 w-full max-w-32 animate-pulse rounded bg-neutral-100" />
                    </td>
                  ))}
                </tr>
              ))
            ) : items.length === 0 ? (
              <tr>
                <td
                  colSpan={5}
                  className="px-4 py-8 text-center text-sm text-neutral-500"
                >
                  Записи не найдены.
                </td>
              </tr>
            ) : (
              items.map((it) => <AuditRow key={it.id} item={it} />)
            )}
          </tbody>
        </table>
      </div>

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

function AuditRow({ item }: { item: AuditLogItem }) {
  const [expanded, setExpanded] = useState(false);
  const hasDetails = item.details && Object.keys(item.details).length > 0;

  return (
    <tr>
      <td className="px-4 py-2 align-top text-xs text-neutral-600">
        {formatDateTime(item.created_at)}
      </td>
      <td className="px-4 py-2 align-top">
        <code className="rounded bg-neutral-100 px-1.5 py-0.5 text-xs font-mono text-neutral-800">
          {item.action}
        </code>
      </td>
      <td className="px-4 py-2 align-top text-xs">
        {item.actor_id ? (
          <Link
            href={`/admin/users/${item.actor_id}`}
            className="text-blue-600 hover:underline"
          >
            {item.actor_email ?? item.actor_id}
          </Link>
        ) : (
          <span className="text-neutral-500">{item.actor_email ?? "—"}</span>
        )}
      </td>
      <td className="px-4 py-2 align-top text-xs text-neutral-700">
        {item.target_type ? (
          <span>
            {item.target_type}
            {item.target_id ? (
              <span className="text-neutral-500"> · {item.target_id}</span>
            ) : null}
          </span>
        ) : (
          "—"
        )}
      </td>
      <td className="px-4 py-2 align-top text-xs">
        {hasDetails ? (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="text-blue-600 hover:underline"
          >
            {expanded ? "Скрыть" : "Показать JSON"}
          </button>
        ) : (
          <span className="text-neutral-400">—</span>
        )}
        {expanded && hasDetails ? (
          <pre className="mt-2 max-w-md overflow-x-auto rounded bg-neutral-900 p-2 text-[11px] text-neutral-100">
            {JSON.stringify(item.details, null, 2)}
          </pre>
        ) : null}
      </td>
    </tr>
  );
}

export default AuditPage;
