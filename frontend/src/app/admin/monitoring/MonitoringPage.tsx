"use client";

import { Spinner } from "@/components/ui/Spinner";
import { formatTokenCount } from "@/lib/formatting";
import type {
  LatencyPercentiles,
  LatencyResponse,
  OpenRouterStatusResponse,
  StorageStatsResponse,
  SystemMonitoringResponse,
} from "@/types/admin";

export interface MonitoringPageProps {
  system: SystemMonitoringResponse | null;
  latency: LatencyResponse | null;
  openrouter: OpenRouterStatusResponse | null;
  storage: StorageStatsResponse | null;
  storageLoading: boolean;
  isLoading: boolean;
  error: string | null;
}

const STAGE_LABELS: Record<string, string> = {
  estimation: "Оценка токенов",
  reservation: "Резервирование",
  openrouter_ttfb: "Время до первого байта",
  openrouter_total: "Время OpenRouter",
  finalization: "Финализация",
  total: "Итого",
};
const STAGE_ORDER = [
  "estimation",
  "reservation",
  "openrouter_ttfb",
  "openrouter_total",
  "finalization",
  "total",
];

function percentTone(percent: number): string {
  if (percent >= 90) return "bg-rose-600";
  if (percent >= 70) return "bg-amber-500";
  return "bg-emerald-600";
}

function hitRateColor(percent: number): string {
  if (percent > 90) return "text-emerald-700";
  if (percent > 70) return "text-amber-600";
  return "text-rose-700";
}

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${seconds} с`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} ч`;
  return `${Math.floor(hours / 24)} дн`;
}

export function MonitoringPage({
  system,
  latency,
  openrouter,
  storage,
  storageLoading,
  isLoading,
  error,
}: MonitoringPageProps) {
  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Мониторинг</h1>
        <p className="text-sm text-neutral-600">
          Обновляется каждые 30 секунд.
        </p>
      </header>

      {error && !system && !latency && !openrouter ? (
        <div
          role="alert"
          className="rounded border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700"
        >
          {error}
        </div>
      ) : null}

      <ResourceCards system={system} isLoading={isLoading && !system} />
      <DatabaseAndRedisRow system={system} isLoading={isLoading && !system} />
      <LatencyTable latency={latency} isLoading={isLoading && !latency} />
      <OpenRouterCard openrouter={openrouter} isLoading={isLoading && !openrouter} />
      <StorageCard storage={storage} isLoading={storageLoading && !storage} />
    </div>
  );
}

const MB = 1024 * 1024;

function formatMb(bytes: number): string {
  return `${(bytes / MB).toFixed(1)} МБ`;
}

function StorageCard({
  storage,
  isLoading,
}: {
  storage: StorageStatsResponse | null;
  isLoading: boolean;
}) {
  const segments = storage
    ? [
        { key: "text", label: "Текст", bytes: storage.text_size_bytes, color: "bg-blue-500" },
        { key: "images", label: "Изображения", bytes: storage.images_size_bytes, color: "bg-purple-500" },
        { key: "files", label: "Файлы", bytes: storage.files_size_bytes, color: "bg-neutral-400" },
      ]
    : [];
  const totalBytes = segments.reduce((sum, s) => sum + s.bytes, 0);

  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-5">
      <h2 className="text-base font-semibold tracking-tight">Хранилище</h2>
      <p className="text-xs text-neutral-500">Обновляется каждые 60 секунд.</p>

      {isLoading || !storage ? (
        <div className="mt-4 h-24 animate-pulse rounded bg-neutral-100" />
      ) : (
        <div className="mt-4 flex flex-col gap-4">
          <div className="flex h-4 w-full overflow-hidden rounded-full bg-neutral-100">
            {totalBytes > 0
              ? segments.map((s) => (
                  <div
                    key={s.key}
                    className={`${s.color} h-full`}
                    style={{ width: `${(s.bytes / totalBytes) * 100}%` }}
                    title={`${s.label}: ${formatMb(s.bytes)}`}
                  />
                ))
              : null}
          </div>

          <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
            {segments.map((s) => (
              <div key={s.key} className="flex items-center gap-2">
                <span className={`inline-block h-3 w-3 rounded-sm ${s.color}`} />
                <span className="text-neutral-600">{s.label}</span>
                <span className="font-mono tabular-nums text-neutral-900">
                  {formatMb(s.bytes)}
                </span>
              </div>
            ))}
          </div>

          <dl className="grid grid-cols-4 gap-3 border-t border-neutral-100 pt-3 text-sm">
            <div>
              <dt className="text-xs text-neutral-500">Изображений</dt>
              <dd className="font-mono tabular-nums">
                {formatTokenCount(storage.total_images_count)}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-neutral-500">Файлов</dt>
              <dd className="font-mono tabular-nums">
                {formatTokenCount(storage.total_files_count)}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-neutral-500">Сообщений</dt>
              <dd className="font-mono tabular-nums">
                {formatTokenCount(storage.total_messages_count)}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-neutral-500">Диалогов</dt>
              <dd className="font-mono tabular-nums">
                {formatTokenCount(storage.total_conversations_count)}
              </dd>
            </div>
          </dl>
        </div>
      )}
    </section>
  );
}

function GaugeCard({
  label,
  percent,
  detail,
  isLoading,
}: {
  label: string;
  percent: number | null;
  detail: string;
  isLoading: boolean;
}) {
  return (
    <div className="rounded-lg border border-neutral-200 bg-white p-5">
      <p className="text-xs uppercase tracking-wide text-neutral-500">
        {label}
      </p>
      {isLoading || percent === null ? (
        <div className="mt-2 h-9 w-32 animate-pulse rounded bg-neutral-200" />
      ) : (
        <>
          <div className="mt-2 flex items-baseline gap-2">
            <span className="text-3xl font-semibold tracking-tight">
              {percent.toFixed(1)}%
            </span>
          </div>
          <div className="mt-2 h-2 w-full overflow-hidden rounded bg-neutral-100">
            <div
              className={`h-full ${percentTone(percent)} transition-[width]`}
              style={{ width: `${Math.min(100, Math.max(0, percent))}%` }}
            />
          </div>
          <p className="mt-2 text-xs text-neutral-500">{detail}</p>
        </>
      )}
    </div>
  );
}

function ResourceCards({
  system,
  isLoading,
}: {
  system: SystemMonitoringResponse | null;
  isLoading: boolean;
}) {
  const sys = system?.system;
  return (
    <section className="grid grid-cols-1 gap-4 md:grid-cols-3">
      <GaugeCard
        label="CPU"
        percent={sys?.cpu_percent ?? null}
        detail="Загрузка процессора"
        isLoading={isLoading}
      />
      <GaugeCard
        label="RAM"
        percent={sys?.ram_percent ?? null}
        detail={
          sys
            ? `${sys.ram_used_mb.toFixed(0)} МБ / ${sys.ram_total_mb.toFixed(0)} МБ`
            : ""
        }
        isLoading={isLoading}
      />
      <GaugeCard
        label="Диск"
        percent={sys?.disk_percent ?? null}
        detail={
          sys
            ? `${sys.disk_used_gb.toFixed(1)} ГБ / ${sys.disk_total_gb.toFixed(1)} ГБ`
            : ""
        }
        isLoading={isLoading}
      />
    </section>
  );
}

function DatabaseAndRedisRow({
  system,
  isLoading,
}: {
  system: SystemMonitoringResponse | null;
  isLoading: boolean;
}) {
  return (
    <section className="grid grid-cols-1 gap-4 md:grid-cols-2">
      <div className="rounded-lg border border-neutral-200 bg-white p-5">
        <h2 className="text-base font-semibold tracking-tight">PostgreSQL</h2>
        {isLoading || !system ? (
          <div className="mt-3 h-16 animate-pulse rounded bg-neutral-100" />
        ) : (
          <dl className="mt-3 grid grid-cols-2 gap-y-1 text-sm">
            <dt className="text-neutral-500">Pool</dt>
            <dd className="text-right tabular-nums">
              {system.database.checked_out} / {system.database.pool_size}
            </dd>
            <dt className="text-neutral-500">Доступно</dt>
            <dd className="text-right tabular-nums">
              {system.database.available}
            </dd>
            <dt className="text-neutral-500">Overflow</dt>
            <dd className="text-right tabular-nums">
              {system.database.overflow}
            </dd>
          </dl>
        )}
      </div>

      <div className="rounded-lg border border-neutral-200 bg-white p-5">
        <h2 className="text-base font-semibold tracking-tight">Redis</h2>
        {isLoading || !system ? (
          <div className="mt-3 h-16 animate-pulse rounded bg-neutral-100" />
        ) : (
          <dl className="mt-3 grid grid-cols-2 gap-y-1 text-sm">
            <dt className="text-neutral-500">Память</dt>
            <dd className="text-right">
              {system.redis.used_memory}
              {system.redis.max_memory !== "0"
                ? ` / ${system.redis.max_memory}`
                : ""}
            </dd>
            <dt className="text-neutral-500">Hit rate</dt>
            <dd
              className={`text-right tabular-nums ${hitRateColor(system.redis.hit_rate_percent)}`}
            >
              {system.redis.hit_rate_percent.toFixed(1)}%
            </dd>
            <dt className="text-neutral-500">Клиенты</dt>
            <dd className="text-right tabular-nums">
              {system.redis.connected_clients}
            </dd>
            <dt className="text-neutral-500">Версия</dt>
            <dd className="text-right font-mono text-xs">
              {system.redis.version}
            </dd>
            <dt className="text-neutral-500">Uptime</dt>
            <dd className="text-right">{formatUptime(system.redis.uptime_seconds)}</dd>
          </dl>
        )}
      </div>
    </section>
  );
}

function LatencyTable({
  latency,
  isLoading,
}: {
  latency: LatencyResponse | null;
  isLoading: boolean;
}) {
  return (
    <section className="rounded-lg border border-neutral-200 bg-white">
      <header className="border-b border-neutral-200 px-4 py-3">
        <h2 className="text-base font-semibold tracking-tight">Задержки</h2>
        <p className="text-xs text-neutral-500">
          Перцентили по выборкам в Redis (мс).
        </p>
      </header>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-neutral-50 text-left text-xs uppercase tracking-wide text-neutral-500">
            <tr>
              <th className="px-4 py-2 font-medium">Этап</th>
              <th className="px-4 py-2 text-right font-medium">P50</th>
              <th className="px-4 py-2 text-right font-medium">P95</th>
              <th className="px-4 py-2 text-right font-medium">P99</th>
              <th className="px-4 py-2 text-right font-medium">Выборка</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-100">
            {isLoading || !latency ? (
              <tr>
                <td colSpan={5} className="px-4 py-6">
                  <div className="flex justify-center">
                    <Spinner />
                  </div>
                </td>
              </tr>
            ) : (
              STAGE_ORDER.map((stage) => (
                <LatencyRow
                  key={stage}
                  stage={stage}
                  data={
                    latency.stages[stage] ?? {
                      p50_ms: null,
                      p95_ms: null,
                      p99_ms: null,
                      sample_count: 0,
                    }
                  }
                />
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function LatencyRow({
  stage,
  data,
}: {
  stage: string;
  data: LatencyPercentiles;
}) {
  const empty = data.sample_count === 0;
  const cls = empty ? "text-neutral-400" : "text-neutral-900";
  const fmt = (n: number | null) =>
    n === null ? "—" : `${Math.round(n).toLocaleString("ru-RU")} мс`;
  return (
    <tr className={cls}>
      <td className="px-4 py-2">{STAGE_LABELS[stage] ?? stage}</td>
      <td className="px-4 py-2 text-right font-mono tabular-nums">
        {fmt(data.p50_ms)}
      </td>
      <td className="px-4 py-2 text-right font-mono tabular-nums">
        {fmt(data.p95_ms)}
      </td>
      <td className="px-4 py-2 text-right font-mono tabular-nums">
        {fmt(data.p99_ms)}
      </td>
      <td className="px-4 py-2 text-right font-mono tabular-nums">
        {formatTokenCount(data.sample_count)}
      </td>
    </tr>
  );
}

function OpenRouterCard({
  openrouter,
  isLoading,
}: {
  openrouter: OpenRouterStatusResponse | null;
  isLoading: boolean;
}) {
  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-5">
      <h2 className="text-base font-semibold tracking-tight">OpenRouter</h2>
      {isLoading || !openrouter ? (
        <div className="mt-3 h-16 animate-pulse rounded bg-neutral-100" />
      ) : (
        <dl className="mt-3 grid grid-cols-2 gap-y-2 text-sm">
          <dt className="text-neutral-500">Баланс</dt>
          <dd
            className={`text-right tabular-nums ${
              openrouter.balance_warning ? "text-rose-700" : "text-neutral-900"
            }`}
          >
            {openrouter.balance_usd === null
              ? "—"
              : `${openrouter.balance_warning ? "⚠ " : ""}$${openrouter.balance_usd.toFixed(2)}`}
          </dd>
          <dt className="text-neutral-500">Активный ключ</dt>
          <dd className="text-right">
            {openrouter.active_key_label ? (
              <>
                {openrouter.active_key_label}{" "}
                <span className="font-mono text-xs text-neutral-500">
                  (…{openrouter.active_key_suffix})
                </span>
              </>
            ) : (
              <span className="text-neutral-400">не задан</span>
            )}
          </dd>
          <dt className="text-neutral-500">Ошибок за час</dt>
          <dd
            className={`text-right tabular-nums ${
              openrouter.recent_error_count_1h > 0
                ? "text-rose-700 font-semibold"
                : "text-neutral-700"
            }`}
          >
            {openrouter.recent_error_count_1h}
          </dd>
        </dl>
      )}
    </section>
  );
}

export default MonitoringPage;
