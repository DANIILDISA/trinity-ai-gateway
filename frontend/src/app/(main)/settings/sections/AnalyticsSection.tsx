"use client";

import { Spinner } from "@/components/ui/Spinner";
import { formatCost, formatTokenCount } from "@/lib/formatting";
import type { AnalyticsResponse, ModelUsageBreakdown } from "@/types/api";
import { EmptyState, Panel, SectionShell } from "./primitives";

export interface AnalyticsSectionProps {
  analytics: AnalyticsResponse | null;
  isLoading: boolean;
}

export function AnalyticsSection({
  analytics,
  isLoading,
}: AnalyticsSectionProps) {
  return (
    <SectionShell
      title="Аналитика"
      description="Совокупная статистика использования аккаунта."
    >
      {isLoading && analytics === null ? (
        <div className="flex justify-center py-12">
          <Spinner size="lg" />
        </div>
      ) : analytics === null ? (
        <Panel>
          <EmptyState>Данные пока недоступны</EmptyState>
        </Panel>
      ) : (
        <>
          <SummaryGrid analytics={analytics} />
          <ByModelPanel rows={analytics.by_model} />
        </>
      )}
    </SectionShell>
  );
}

function SummaryGrid({ analytics }: { analytics: AnalyticsResponse }) {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <StatCard
        label="Сообщений"
        value={formatTokenCount(analytics.total_messages)}
        tone="neutral"
      />
      <StatCard
        label="Токены входа"
        value={formatTokenCount(analytics.total_input_tokens)}
        tone="neutral"
      />
      <StatCard
        label="Токены выхода"
        value={formatTokenCount(analytics.total_output_tokens)}
        tone="neutral"
      />
      <StatCard
        label="Всего потрачено"
        value={formatCost(analytics.total_spent)}
        tone="accent"
      />
    </div>
  );
}

function StatCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "neutral" | "accent";
}) {
  return (
    <div className="overflow-hidden rounded-lg border border-neutral-800 bg-neutral-900 px-4 py-4">
      <p className="font-mono text-[10px] uppercase tracking-wider text-neutral-600">
        {label}
      </p>
      <p
        className={`mt-2 font-mono text-2xl font-semibold tabular-nums tracking-tight ${
          tone === "accent" ? "text-emerald-400" : "text-neutral-100"
        }`}
      >
        {value}
      </p>
    </div>
  );
}

function ByModelPanel({ rows }: { rows: ModelUsageBreakdown[] }) {
  return (
    <Panel title="Разбивка по моделям">
      {rows.length === 0 ? (
        <EmptyState>Нет данных по моделям</EmptyState>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-left text-sm">
            <thead>
              <tr className="border-b border-neutral-800 font-mono text-[10px] uppercase tracking-wider text-neutral-600">
                <th className="py-2 pr-3 font-medium">Модель</th>
                <th className="py-2 pr-3 text-right font-medium">Сообщ.</th>
                <th className="py-2 pr-3 text-right font-medium">Вход</th>
                <th className="py-2 pr-3 text-right font-medium">Выход</th>
                <th className="py-2 pr-3 text-right font-medium">Потрачено</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-800/60">
              {rows.map((row) => (
                <tr key={row.model_id} className="text-neutral-300">
                  <td className="py-3 pr-3">
                    <span className="font-mono text-xs text-neutral-200">
                      {row.model_id}
                    </span>
                  </td>
                  <td className="py-3 pr-3 text-right font-mono text-sm tabular-nums">
                    {formatTokenCount(row.messages)}
                  </td>
                  <td className="py-3 pr-3 text-right font-mono text-sm tabular-nums text-neutral-300">
                    {formatTokenCount(row.input_tokens)}
                  </td>
                  <td className="py-3 pr-3 text-right font-mono text-sm tabular-nums text-neutral-300">
                    {formatTokenCount(row.output_tokens)}
                  </td>
                  <td className="py-3 pr-3 text-right font-mono text-sm tabular-nums text-emerald-400">
                    {formatCost(row.spent)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
