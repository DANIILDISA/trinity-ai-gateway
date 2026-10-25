"use client";

import Link from "next/link";

import { Spinner } from "@/components/ui/Spinner";
import {
  formatDateTime,
  formatPriceShort,
  formatTokenCount,
} from "@/lib/formatting";
import type { ModelInfo } from "@/types/api";

export interface PricingTableProps {
  models: ModelInfo[];
  lastUpdate: string | null;
  isLoading: boolean;
  error: string | null;
}

export function PricingTable({
  models,
  lastUpdate,
  isLoading,
  error,
}: PricingTableProps) {
  return (
    <main className="mx-auto flex w-full max-w-5xl flex-col gap-6 px-4 py-8">
      <header className="flex flex-col gap-1">
        <Link
          href="/"
          className="mb-2 inline-block self-start text-sm text-neutral-400 transition-colors hover:text-neutral-100 hover:underline"
        >
          ← На главную
        </Link>
        <h1 className="text-3xl font-semibold tracking-tight text-neutral-100">
          Тарифы
        </h1>
        <p className="text-sm text-neutral-400">
          Цены указаны за 1 млн токенов.
        </p>
        {lastUpdate ? (
          <p className="text-xs text-neutral-500">
            Последнее обновление цен: {formatDateTime(lastUpdate)}
          </p>
        ) : null}
      </header>

      {isLoading ? (
        <div className="flex justify-center py-12">
          <Spinner size="lg" />
        </div>
      ) : error ? (
        <p
          role="alert"
          className="rounded-lg border border-red-800 bg-red-900/30 px-4 py-3 text-sm text-red-400"
        >
          {error}
        </p>
      ) : models.length === 0 ? (
        <p className="rounded-lg border border-neutral-800 bg-neutral-900 px-4 py-6 text-sm text-neutral-400">
          Модели не найдены.
        </p>
      ) : (
        <>
          <DesktopTable models={models} />
          <MobileCards models={models} />
        </>
      )}
    </main>
  );
}

function DesktopTable({ models }: { models: ModelInfo[] }) {
  return (
    <div className="hidden overflow-x-auto rounded-lg border border-neutral-800 bg-neutral-900 md:block">
      <table className="w-full text-sm">
        <thead className="border-b border-neutral-800 bg-neutral-950/50 text-left text-xs uppercase tracking-wide text-neutral-500">
          <tr>
            <th className="px-4 py-3 font-medium">Модель</th>
            <th className="px-4 py-3 font-medium">Провайдер</th>
            <th className="px-4 py-3 text-right font-medium">
              Входящие (за 1М)
            </th>
            <th className="px-4 py-3 text-right font-medium">
              Исходящие (за 1М)
            </th>
            <th className="px-4 py-3 text-right font-medium">Контекст</th>
            <th className="px-4 py-3 font-medium">Статус</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-neutral-800">
          {models.map((m) => (
            <tr key={m.id} className="transition-colors hover:bg-neutral-800/40">
              <td className="px-4 py-3">
                <div className="font-medium text-neutral-100">{m.name}</div>
                <div className="text-xs text-neutral-500">{m.id}</div>
              </td>
              <td className="px-4 py-3 text-neutral-300">{m.provider}</td>
              <td className="px-4 py-3 text-right font-mono tabular-nums text-neutral-200">
                {formatPriceShort(m.input_price_per_m)}
              </td>
              <td className="px-4 py-3 text-right font-mono tabular-nums text-neutral-200">
                {formatPriceShort(m.output_price_per_m)}
              </td>
              <td className="px-4 py-3 text-right tabular-nums text-neutral-300">
                {formatTokenCount(m.context_window)}
              </td>
              <td className="px-4 py-3">
                <StatusBadge status={m.status} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MobileCards({ models }: { models: ModelInfo[] }) {
  return (
    <ul className="flex flex-col gap-3 md:hidden">
      {models.map((m) => (
        <li
          key={m.id}
          className="rounded-lg border border-neutral-800 bg-neutral-900 p-4 text-sm"
        >
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="truncate font-medium text-neutral-100">
                {m.name}
              </div>
              <div className="truncate text-xs text-neutral-500">{m.id}</div>
              <div className="mt-1 text-xs text-neutral-400">{m.provider}</div>
            </div>
            <StatusBadge status={m.status} />
          </div>

          <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
            <dt className="text-neutral-500">Входящие</dt>
            <dd className="text-right font-mono tabular-nums text-neutral-200">
              {formatPriceShort(m.input_price_per_m)}
            </dd>
            <dt className="text-neutral-500">Исходящие</dt>
            <dd className="text-right font-mono tabular-nums text-neutral-200">
              {formatPriceShort(m.output_price_per_m)}
            </dd>
            <dt className="text-neutral-500">Контекст</dt>
            <dd className="text-right tabular-nums text-neutral-300">
              {formatTokenCount(m.context_window)}
            </dd>
          </dl>
        </li>
      ))}
    </ul>
  );
}

function StatusBadge({ status }: { status: string }) {
  const isAvailable = status === "available";
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
        isAvailable
          ? "bg-emerald-600/20 text-emerald-400"
          : "bg-neutral-800 text-neutral-400"
      }`}
    >
      {isAvailable ? "Доступна" : "Недоступна"}
    </span>
  );
}

export default PricingTable;
