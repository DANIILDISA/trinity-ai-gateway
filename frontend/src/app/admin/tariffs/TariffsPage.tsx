"use client";

import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import { formatBalance, formatDateTime } from "@/lib/formatting";
import type {
  CbrRateResponse,
  PricePreview,
  SyncResult,
  TariffsResponse,
  TariffModel,
} from "@/types/admin";

export interface TariffsPageProps {
  tariffs: TariffsResponse | null;
  cbrRate: CbrRateResponse | null;
  preview: PricePreview[] | null;

  rateInput: string;
  rateInputError: string | null;

  isLoadingTariffs: boolean;
  isLoadingPreview: boolean;
  isApplying: boolean;

  applyError: string | null;
  applySuccess: string | null;

  cooldownInfo: { active: boolean; nextAllowedAt: string | null };

  isSyncing: boolean;
  syncResult: SyncResult | null;
  syncError: boolean;

  onRateInputChange: (value: string) => void;
  onPreview: () => void;
  onApply: () => void;
  onSyncModels: () => void;
  onOverrideModel: (
    modelId: string,
    inputRub: number,
    outputRub: number,
  ) => Promise<void>;
  onClearOverride: (modelId: string) => Promise<void>;
}

export function TariffsPage(props: TariffsPageProps) {
  const {
    tariffs,
    cbrRate,
    preview,
    rateInput,
    rateInputError,
    isLoadingTariffs,
    isLoadingPreview,
    isApplying,
    applyError,
    applySuccess,
    isSyncing,
    syncResult,
    syncError,
    onRateInputChange,
    onPreview,
    onApply,
    onSyncModels,
    onOverrideModel,
    onClearOverride,
  } = props;

  if (isLoadingTariffs && !tariffs) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <Spinner size="lg" />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-semibold tracking-tight">Тарифы</h1>

      <CurrentRateCard tariffs={tariffs} cbrRate={cbrRate} />

      <RateUpdateCard
        rateInput={rateInput}
        rateInputError={rateInputError}
        onRateInputChange={onRateInputChange}
        onPreview={onPreview}
        onApply={onApply}
        isLoadingPreview={isLoadingPreview}
        isApplying={isApplying}
        applyError={applyError}
        applySuccess={applySuccess}
      />

      <ModelSyncCard
        isSyncing={isSyncing}
        syncResult={syncResult}
        syncError={syncError}
        onSyncModels={onSyncModels}
      />

      {preview ? <PreviewTable rows={preview} /> : null}

      <CurrentPricesTable
        models={tariffs?.models ?? []}
        onOverrideModel={onOverrideModel}
        onClearOverride={onClearOverride}
      />
    </div>
  );
}

function CurrentRateCard({
  tariffs,
  cbrRate,
}: {
  tariffs: TariffsResponse | null;
  cbrRate: CbrRateResponse | null;
}) {
  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-6">
      <h2 className="text-lg font-semibold tracking-tight">Текущие настройки</h2>
      <dl className="mt-3 grid grid-cols-1 gap-x-6 gap-y-2 text-sm md:grid-cols-2">
        <Item
          label="Курс USD/RUB"
          value={
            tariffs ? `${formatBalance(tariffs.exchange_rate_display)} / USD` : "—"
          }
        />
        <Item
          label="Наценка"
          value={
            tariffs
              ? `${((tariffs.markup_multiplier - 1) * 100).toFixed(0)}% (×${tariffs.markup_multiplier.toFixed(2)})`
              : "—"
          }
        />
        <Item
          label="Последнее обновление"
          value={tariffs?.last_updated ? formatDateTime(tariffs.last_updated) : "—"}
        />
        <Item
          label="Следующее доступно"
          value={
            tariffs?.next_update_allowed_at
              ? formatDateTime(tariffs.next_update_allowed_at)
              : "—"
          }
        />
        <Item
          label="Курс ЦБ (справочно)"
          value={cbrRate ? `${cbrRate.rate_usd_rub.toFixed(2)} ₽ / USD` : "—"}
          hint={cbrRate?.note}
        />
      </dl>
    </section>
  );
}

function Item({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-neutral-500">
        {label}
      </dt>
      <dd className="text-sm text-neutral-900">{value}</dd>
      {hint ? <p className="text-[11px] text-neutral-400">{hint}</p> : null}
    </div>
  );
}

function RateUpdateCard({
  rateInput,
  rateInputError,
  onRateInputChange,
  onPreview,
  onApply,
  isLoadingPreview,
  isApplying,
  applyError,
  applySuccess,
}: Pick<
  TariffsPageProps,
  | "rateInput"
  | "rateInputError"
  | "onRateInputChange"
  | "onPreview"
  | "onApply"
  | "isLoadingPreview"
  | "isApplying"
  | "applyError"
  | "applySuccess"
>) {
  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-6">
      <h2 className="text-lg font-semibold tracking-tight">
        Обновление курса
      </h2>

      <div className="mt-4 flex flex-col gap-3 sm:max-w-md">
        <Input
          label="Новый курс USD/RUB (в рублях, например 85)"
          type="number"
          inputMode="decimal"
          step={0.0001}
          value={rateInput}
          onChange={(e) => onRateInputChange(e.target.value)}
          error={rateInputError ?? undefined}
          disabled={isApplying}
        />
        <div className="flex gap-2">
          <Button
            variant="secondary"
            onClick={onPreview}
            loading={isLoadingPreview}
            disabled={isLoadingPreview || isApplying}
          >
            Предпросмотр
          </Button>
          <Button onClick={onApply} loading={isApplying} disabled={isApplying}>
            Применить
          </Button>
        </div>

        {applySuccess ? (
          <p
            role="status"
            className="rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800"
          >
            {applySuccess}
          </p>
        ) : null}
        {applyError ? (
          <p role="alert" className="text-sm text-rose-600">
            {applyError}
          </p>
        ) : null}
      </div>
    </section>
  );
}

function ModelSyncCard({
  isSyncing,
  syncResult,
  syncError,
  onSyncModels,
}: Pick<
  TariffsPageProps,
  "isSyncing" | "syncResult" | "syncError" | "onSyncModels"
>) {
  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-6">
      <h2 className="text-lg font-semibold tracking-tight">Модели OpenRouter</h2>
      <p className="mt-1 text-sm text-neutral-600">
        Синхронизировать список моделей и цены с OpenRouter API
      </p>

      <div className="mt-4 flex flex-col gap-3">
        <div>
          <Button onClick={onSyncModels} loading={isSyncing} disabled={isSyncing}>
            {isSyncing ? "Синхронизация..." : "🔄 Синхронизировать модели"}
          </Button>
        </div>

        {syncResult ? (
          <div className="flex flex-col gap-1">
            <p role="status" className="text-sm font-medium text-emerald-700">
              ✅ Добавлено: {syncResult.added} · Обновлено: {syncResult.updated} ·
              Деактивировано: {syncResult.deactivated}
            </p>
            {syncResult.errors.length > 0 ? (
              <ul className="list-disc pl-5 text-xs text-rose-600">
                {syncResult.errors.map((e, i) => (
                  <li key={i}>{e}</li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}

        {syncError ? (
          <p role="alert" className="text-sm text-rose-600">
            ❌ Ошибка синхронизации
          </p>
        ) : null}
      </div>
    </section>
  );
}

function PreviewTable({ rows }: { rows: PricePreview[] }) {
  return (
    <section className="rounded-lg border border-neutral-200 bg-white">
      <header className="border-b border-neutral-200 px-4 py-3">
        <h2 className="text-base font-semibold tracking-tight">Предпросмотр</h2>
        <p className="text-xs text-neutral-500">
          Сравнение текущих цен с предложенным курсом. Зафиксированные модели не меняются при применении.
        </p>
      </header>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-neutral-50 text-left text-xs uppercase tracking-wide text-neutral-500">
            <tr>
              <th className="px-4 py-2 font-medium">Модель</th>
              <th className="px-4 py-2 text-right font-medium">Текущий вход</th>
              <th className="px-4 py-2 text-right font-medium">Новый вход</th>
              <th className="px-4 py-2 text-right font-medium">Текущий выход</th>
              <th className="px-4 py-2 text-right font-medium">Новый выход</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-100">
            {rows.map((r) => (
              <PreviewRow key={r.model_id} row={r} />
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function PreviewRow({ row }: { row: PricePreview }) {
  const inDelta = compareDecimal(row.current_input_price, row.proposed_input_price);
  const outDelta = compareDecimal(row.current_output_price, row.proposed_output_price);

  const baseClass = row.is_override ? "italic text-neutral-500" : "text-neutral-900";

  return (
    <tr className={baseClass}>
      <td className="px-4 py-2">
        <div className="font-medium">{row.model_name}</div>
        <div className="text-xs text-neutral-500">
          {row.model_id}
          {row.is_override ? (
            <span className="ml-2 inline-flex items-center rounded-full bg-neutral-200 px-2 py-0.5 text-[10px] font-medium text-neutral-700">
              🔒 Зафиксировано вручную
            </span>
          ) : null}
        </div>
      </td>
      <td className="px-4 py-2 text-right font-mono tabular-nums">
        {formatBalance(row.current_input_price)}
      </td>
      <td className={`px-4 py-2 text-right font-mono tabular-nums ${row.is_override ? "" : deltaClass(inDelta)}`}>
        {formatBalance(row.proposed_input_price)}
      </td>
      <td className="px-4 py-2 text-right font-mono tabular-nums">
        {formatBalance(row.current_output_price)}
      </td>
      <td className={`px-4 py-2 text-right font-mono tabular-nums ${row.is_override ? "" : deltaClass(outDelta)}`}>
        {formatBalance(row.proposed_output_price)}
      </td>
    </tr>
  );
}

function compareDecimal(currentApi: string, proposedApi: string): number {
  return Number(proposedApi) - Number(currentApi);
}

function deltaClass(delta: number): string {
  if (delta > 0) return "text-rose-600";
  if (delta < 0) return "text-emerald-700";
  return "text-neutral-900";
}

function CurrentPricesTable({
  models,
  onOverrideModel,
  onClearOverride,
}: {
  models: TariffModel[];
  onOverrideModel: TariffsPageProps["onOverrideModel"];
  onClearOverride: TariffsPageProps["onClearOverride"];
}) {
  return (
    <section className="rounded-lg border border-neutral-200 bg-white">
      <header className="border-b border-neutral-200 px-4 py-3">
        <h2 className="text-base font-semibold tracking-tight">
          Текущие цены
        </h2>
      </header>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-neutral-50 text-left text-xs uppercase tracking-wide text-neutral-500">
            <tr>
              <th className="px-4 py-2 font-medium">Модель</th>
              <th className="px-4 py-2 font-medium">Провайдер</th>
              <th className="px-4 py-2 text-right font-medium">Вход</th>
              <th className="px-4 py-2 text-right font-medium">Выход</th>
              <th className="px-4 py-2 font-medium">Статус</th>
              <th className="px-4 py-2 text-right font-medium">Действие</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-100">
            {models.map((m) => (
              <CurrentPriceRow
                key={m.model_id}
                model={m}
                onOverrideModel={onOverrideModel}
                onClearOverride={onClearOverride}
              />
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function CurrentPriceRow({
  model,
  onOverrideModel,
  onClearOverride,
}: {
  model: TariffModel;
  onOverrideModel: TariffsPageProps["onOverrideModel"];
  onClearOverride: TariffsPageProps["onClearOverride"];
}) {
  const [editing, setEditing] = useState(false);
  const [inputRub, setInputRub] = useState(
    (model.input_price_micro_rub / 1_000_000).toFixed(4),
  );
  const [outputRub, setOutputRub] = useState(
    (model.output_price_micro_rub / 1_000_000).toFixed(4),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSave = async () => {
    setError(null);
    const inVal = Number(inputRub);
    const outVal = Number(outputRub);
    if (!Number.isFinite(inVal) || inVal < 0 || !Number.isFinite(outVal) || outVal < 0) {
      setError("Некорректные значения.");
      return;
    }
    setSaving(true);
    try {
      await onOverrideModel(
        model.model_id,
        Math.round(inVal * 1_000_000),
        Math.round(outVal * 1_000_000),
      );
      setEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сохранить.");
    } finally {
      setSaving(false);
    }
  };

  const handleUnlock = async () => {
    setError(null);
    setSaving(true);
    try {
      await onClearOverride(model.model_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось снять фиксацию.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <tr>
      <td className="px-4 py-2">
        <div className="font-medium text-neutral-900">{model.model_name}</div>
        <div className="text-xs text-neutral-500">
          {model.model_id}
          {model.is_price_override ? (
            <span className="ml-2 inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium text-amber-800">
              🔒 Фикс
            </span>
          ) : null}
        </div>
      </td>
      <td className="px-4 py-2 text-neutral-700">{model.provider}</td>
      <td className="px-4 py-2 text-right">
        {editing ? (
          <input
            type="number"
            step="0.0001"
            value={inputRub}
            onChange={(e) => setInputRub(e.target.value)}
            className="w-28 rounded border border-neutral-300 px-2 py-1 text-right text-sm"
          />
        ) : (
          <span className="font-mono tabular-nums">
            {formatBalance(model.input_price_display)}
          </span>
        )}
      </td>
      <td className="px-4 py-2 text-right">
        {editing ? (
          <input
            type="number"
            step="0.0001"
            value={outputRub}
            onChange={(e) => setOutputRub(e.target.value)}
            className="w-28 rounded border border-neutral-300 px-2 py-1 text-right text-sm"
          />
        ) : (
          <span className="font-mono tabular-nums">
            {formatBalance(model.output_price_display)}
          </span>
        )}
      </td>
      <td className="px-4 py-2">
        <span
          className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
            model.status === "available"
              ? "bg-emerald-100 text-emerald-800"
              : "bg-neutral-200 text-neutral-700"
          }`}
        >
          {model.status === "available" ? "Доступна" : "Недоступна"}
        </span>
      </td>
      <td className="px-4 py-2 text-right">
        {editing ? (
          <div className="flex justify-end gap-2">
            <Button
              size="sm"
              variant="secondary"
              onClick={() => setEditing(false)}
              disabled={saving}
            >
              Отмена
            </Button>
            <Button size="sm" onClick={handleSave} loading={saving} disabled={saving}>
              Сохранить
            </Button>
          </div>
        ) : model.is_price_override ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => void handleUnlock()}
            loading={saving}
            disabled={saving}
          >
            Снять фиксацию
          </Button>
        ) : (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setError(null);
              setInputRub((model.input_price_micro_rub / 1_000_000).toFixed(4));
              setOutputRub((model.output_price_micro_rub / 1_000_000).toFixed(4));
              setEditing(true);
            }}
          >
            Зафиксировать
          </Button>
        )}
        {error ? (
          <p className="mt-1 text-right text-xs text-rose-600">{error}</p>
        ) : null}
      </td>
    </tr>
  );
}

export default TariffsPage;
