"use client";

import { useCallback, useEffect, useState } from "react";

import { Spinner } from "@/components/ui/Spinner";
import { getRequests } from "@/lib/adminApi";
import { formatCost, formatDateTime, formatTokenCount } from "@/lib/formatting";
import type { RequestListResponse } from "@/types/admin";

const PER_PAGE = 20;

const STATUS_LABELS: Record<string, string> = {
  completed: "Завершён",
  partial: "Частичный",
  failed: "Ошибка",
  refunded: "Возврат",
};

export default function AdminRequestsPage() {
  const [data, setData] = useState<RequestListResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");

  const load = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const res = await getRequests({
        page,
        per_page: PER_PAGE,
        request_id: search || undefined,
      });
      setData(res);
    } catch {
      setError("Не удалось загрузить запросы.");
    } finally {
      setIsLoading(false);
    }
  }, [page, search]);

  useEffect(() => {
    void load();
  }, [load]);

  const onSearch = (e: React.FormEvent) => {
    e.preventDefault();
    setPage(1);
    setSearch(searchInput.trim());
  };

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PER_PAGE)) : 1;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold tracking-tight">Запросы</h1>
        <form onSubmit={onSearch} className="flex gap-2">
          <input
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            placeholder="Поиск по ID запроса"
            className="w-64 rounded border border-neutral-300 px-3 py-1.5 text-sm outline-none focus:border-neutral-900"
          />
          <button
            type="submit"
            className="rounded bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-neutral-800"
          >
            Найти
          </button>
        </form>
      </div>

      {error ? (
        <p className="rounded border border-rose-200 bg-rose-50 px-4 py-2 text-sm text-rose-700">
          {error}
        </p>
      ) : null}

      <div className="overflow-x-auto rounded-lg border border-neutral-200 bg-white">
        <table className="w-full text-sm">
          <thead className="bg-neutral-50 text-left text-xs uppercase tracking-wide text-neutral-500">
            <tr>
              <th className="px-3 py-2 font-medium">ID</th>
              <th className="px-3 py-2 font-medium">Дата/время</th>
              <th className="px-3 py-2 font-medium">Пользователь</th>
              <th className="px-3 py-2 font-medium">Модель</th>
              <th className="px-3 py-2 font-medium">Статус</th>
              <th className="px-3 py-2 text-right font-medium">Токены вх/исх</th>
              <th className="px-3 py-2 text-right font-medium">Стоимость нам</th>
              <th className="px-3 py-2 text-right font-medium">Пользователю</th>
              <th className="px-3 py-2 text-right font-medium">Символов</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-100">
            {isLoading && !data ? (
              <tr>
                <td colSpan={9} className="px-3 py-8">
                  <div className="flex justify-center">
                    <Spinner />
                  </div>
                </td>
              </tr>
            ) : data && data.items.length === 0 ? (
              <tr>
                <td colSpan={9} className="px-3 py-8 text-center text-neutral-500">
                  Запросы не найдены.
                </td>
              </tr>
            ) : (
              data?.items.map((r) => (
                <tr key={r.request_id} className="hover:bg-neutral-50">
                  <td className="px-3 py-2 font-mono text-xs" title={r.request_id}>
                    {r.request_id.slice(0, 8)}
                  </td>
                  <td className="px-3 py-2 whitespace-nowrap text-xs text-neutral-600">
                    {formatDateTime(r.created_at)}
                  </td>
                  <td className="px-3 py-2 text-xs">{r.user_email ?? "—"}</td>
                  <td className="px-3 py-2 text-xs">{r.model_name ?? r.model_id}</td>
                  <td className="px-3 py-2 text-xs">
                    {STATUS_LABELS[r.status] ?? r.status}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-xs tabular-nums">
                    {formatTokenCount(r.input_tokens)} / {formatTokenCount(r.output_tokens)}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-xs tabular-nums">
                    ${r.openrouter_cost_usd.toFixed(4)}
                    <span className="block text-neutral-400">
                      {formatCost(r.our_cost_display)}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-xs tabular-nums">
                    {formatCost(r.user_cost_display)}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-xs tabular-nums">
                    {r.prompt_length_chars ?? "—"}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between text-sm text-neutral-600">
        <span>{data ? `Всего: ${data.total}` : ""}</span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            className="rounded border border-neutral-300 px-3 py-1 disabled:opacity-40"
          >
            Назад
          </button>
          <span>
            {page} / {totalPages}
          </span>
          <button
            type="button"
            disabled={page >= totalPages}
            onClick={() => setPage((p) => p + 1)}
            className="rounded border border-neutral-300 px-3 py-1 disabled:opacity-40"
          >
            Вперёд
          </button>
        </div>
      </div>
    </div>
  );
}
