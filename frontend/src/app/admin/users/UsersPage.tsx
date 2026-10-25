"use client";

import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { formatBalance, formatDateTime } from "@/lib/formatting";
import type {
  AdminUserSortBy,
  AdminUserSortOrder,
  AdminUserSummary,
} from "@/types/admin";

export interface UsersPageProps {
  users: AdminUserSummary[];
  total: number;
  page: number;
  perPage: number;
  searchQuery: string;
  sortBy: AdminUserSortBy;
  sortOrder: AdminUserSortOrder;
  isLoading: boolean;
  error: string | null;
  onSearchChange: (value: string) => void;
  onSort: (column: AdminUserSortBy) => void;
  onPageChange: (page: number) => void;
}

const SORTABLE_COLUMNS = new Set<AdminUserSortBy>([
  "balance_micro_rub",
  "last_active_at",
  "created_at",
]);

export function UsersPage({
  users,
  total,
  page,
  perPage,
  searchQuery,
  sortBy,
  sortOrder,
  isLoading,
  error,
  onSearchChange,
  onSort,
  onPageChange,
}: UsersPageProps) {
  const router = useRouter();
  const totalPages = Math.max(1, Math.ceil(total / perPage));

  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            Пользователи
          </h1>
          <p className="text-sm text-neutral-600">
            Поиск, сортировка, переход к карточке пользователя.
          </p>
        </div>
        <input
          type="search"
          value={searchQuery}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder="Поиск по email или имени"
          className="h-9 w-72 rounded border border-neutral-300 bg-white px-3 text-sm outline-none focus:border-neutral-900"
        />
      </header>

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
          <thead className="border-b border-neutral-200 bg-neutral-50 text-left text-xs uppercase tracking-wide text-neutral-500">
            <tr>
              <th className="px-4 py-2 font-medium">Email</th>
              <th className="px-4 py-2 font-medium">Имя</th>
              <SortableTh
                column="balance_micro_rub"
                label="Баланс"
                align="right"
                currentSort={sortBy}
                currentOrder={sortOrder}
                onSort={onSort}
              />
              <th className="px-4 py-2 font-medium">Статус</th>
              <SortableTh
                column="last_active_at"
                label="Последняя активность"
                currentSort={sortBy}
                currentOrder={sortOrder}
                onSort={onSort}
              />
              <SortableTh
                column="created_at"
                label="Дата регистрации"
                currentSort={sortBy}
                currentOrder={sortOrder}
                onSort={onSort}
              />
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-100">
            {isLoading && users.length === 0 ? (
              Array.from({ length: 5 }).map((_, i) => (
                <tr key={`s-${i}`}>
                  {Array.from({ length: 6 }).map((__, j) => (
                    <td key={j} className="px-4 py-3">
                      <span className="inline-block h-4 w-full max-w-32 animate-pulse rounded bg-neutral-100" />
                    </td>
                  ))}
                </tr>
              ))
            ) : users.length === 0 ? (
              <tr>
                <td
                  colSpan={6}
                  className="px-4 py-8 text-center text-sm text-neutral-500"
                >
                  Пользователи не найдены.
                </td>
              </tr>
            ) : (
              users.map((u) => (
                <tr
                  key={u.id}
                  onClick={() => router.push(`/admin/users/${u.id}`)}
                  className="cursor-pointer hover:bg-neutral-50"
                >
                  <td className="px-4 py-3 font-medium text-neutral-900">
                    {u.email}
                  </td>
                  <td className="px-4 py-3 text-neutral-700">
                    {u.display_name}
                  </td>
                  <td className="px-4 py-3 text-right font-mono tabular-nums">
                    {formatBalance(u.balance)}
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge status={u.status} />
                  </td>
                  <td className="px-4 py-3 text-xs text-neutral-600">
                    {u.last_active_at ? formatDateTime(u.last_active_at) : "—"}
                  </td>
                  <td className="px-4 py-3 text-xs text-neutral-600">
                    {formatDateTime(u.created_at)}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <footer className="flex items-center justify-between">
        <span className="text-xs text-neutral-500">
          {isLoading && users.length === 0
            ? "Загрузка..."
            : `Найдено: ${total.toLocaleString("ru-RU")}`}
        </span>
        <div className="flex items-center gap-2 text-sm">
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

function SortableTh({
  column,
  label,
  align = "left",
  currentSort,
  currentOrder,
  onSort,
}: {
  column: AdminUserSortBy;
  label: string;
  align?: "left" | "right";
  currentSort: AdminUserSortBy;
  currentOrder: AdminUserSortOrder;
  onSort: (column: AdminUserSortBy) => void;
}) {
  const active = column === currentSort;
  const arrow = active ? (currentOrder === "asc" ? " ↑" : " ↓") : "";
  const isSortable = SORTABLE_COLUMNS.has(column);
  const alignClass = align === "right" ? "text-right" : "text-left";

  if (!isSortable) {
    return (
      <th className={`px-4 py-2 font-medium ${alignClass}`}>{label}</th>
    );
  }

  return (
    <th className={`px-4 py-2 font-medium ${alignClass}`}>
      <button
        type="button"
        onClick={() => onSort(column)}
        className="inline-flex items-center gap-1 uppercase tracking-wide text-neutral-500 hover:text-neutral-900"
      >
        {label}
        <span aria-hidden="true">{arrow}</span>
      </button>
    </th>
  );
}

function StatusBadge({ status }: { status: string }) {
  const active = status === "active";
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
        active
          ? "bg-emerald-100 text-emerald-800"
          : "bg-rose-100 text-rose-800"
      }`}
    >
      {active ? "Активен" : "Заблокирован"}
    </span>
  );
}

export default UsersPage;
