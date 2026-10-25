"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import type { SystemSettings } from "@/types/admin";

type FieldType = "int" | "float" | "bool" | "select";

interface FieldDef {
  key: string;
  label: string;
  hint?: string;
  type: FieldType;
  options?: { value: string; label: string }[];
}

type SettingValue = number | boolean | string;

interface SectionDef {
  title: string;
  hint?: string;
  group: keyof SystemSettings;
  fields: FieldDef[];
}

const SECTIONS: SectionDef[] = [
  {
    title: "Лимиты запросов",
    hint: "Изменения вступят в силу в течение 60 секунд.",
    group: "rate_limits",
    fields: [
      { key: "rate_limit_per_minute", label: "Запросов в минуту", type: "int" },
      { key: "rate_limit_per_hour", label: "Запросов в час", type: "int" },
      { key: "rate_limit_per_day", label: "Запросов в день", type: "int" },
    ],
  },
  {
    title: "Биллинг",
    group: "billing",
    fields: [
      {
        key: "reservation_buffer_multiplier",
        label: "Множитель резервации",
        hint: "например, 2.0 = резервируем 2× от оценки",
        type: "float",
      },
      {
        key: "overdraft_tolerance_percent",
        label: "Допуск overdraft (%)",
        type: "float",
      },
      {
        key: "low_balance_remaining_requests",
        label: "Предупреждение при N запросах остатка",
        type: "int",
      },
      {
        key: "markup_multiplier",
        label: "Наценка (множитель)",
        hint: "1.25 = +25%. Применяется к цене OpenRouter.",
        type: "float",
      },
    ],
  },
  {
    title: "Платежи",
    group: "payments",
    fields: [
      { key: "min_topup_amount_rub", label: "Мин. пополнение (₽)", type: "int" },
    ],
  },
  {
    title: "OpenRouter",
    group: "openrouter",
    fields: [
      {
        key: "openrouter_balance_threshold_usd",
        label: "Порог баланса (USD)",
        type: "float",
      },
    ],
  },
  {
    title: "Сверка",
    group: "reconciliation",
    fields: [
      {
        key: "reconciliation_threshold_percent",
        label: "Порог расхождения (%)",
        type: "float",
      },
    ],
  },
  {
    title: "Автообновление курса",
    hint: "Курс ЦБ загружается автоматически. Задача проверяет интервал и пропускает запуск, если время ещё не пришло.",
    group: "exchange_rate",
    fields: [
      {
        key: "auto_exchange_rate_enabled",
        label: "Включить автообновление",
        type: "bool",
      },
      {
        key: "auto_exchange_rate_interval_minutes",
        label: "Интервал обновления (минут)",
        hint: "1440 = раз в сутки",
        type: "int",
      },
      {
        key: "auto_exchange_rate_mode",
        label: "Режим",
        hint: "Автоматически — применять сразу; Ручное — создавать заявку на подтверждение.",
        type: "select",
        options: [
          { value: "auto", label: "Автоматически" },
          { value: "manual_confirm", label: "Ручное подтверждение" },
        ],
      },
    ],
  },
];

export interface SettingsPageProps {
  settings: SystemSettings | null;
  isLoading: boolean;
  loadError: string | null;
  savingKey: string | null;
  errorByKey: Record<string, string | null>;
  successByKey: Record<string, number | null>; // timestamp
  onSave: (key: string, value: SettingValue) => void;
}

export function SettingsPage({
  settings,
  isLoading,
  loadError,
  savingKey,
  errorByKey,
  successByKey,
  onSave,
}: SettingsPageProps) {
  if (isLoading && !settings) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <Spinner size="lg" />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-semibold tracking-tight">Настройки</h1>

      {loadError ? (
        <div
          role="alert"
          className="rounded border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700"
        >
          {loadError}
        </div>
      ) : null}

      {SECTIONS.map((section) => (
        <SettingsSection
          key={section.title}
          section={section}
          settings={settings}
          savingKey={savingKey}
          errorByKey={errorByKey}
          successByKey={successByKey}
          onSave={onSave}
        />
      ))}
    </div>
  );
}

function SettingsSection({
  section,
  settings,
  savingKey,
  errorByKey,
  successByKey,
  onSave,
}: {
  section: SectionDef;
  settings: SystemSettings | null;
  savingKey: string | null;
  errorByKey: Record<string, string | null>;
  successByKey: Record<string, number | null>;
  onSave: (key: string, value: SettingValue) => void;
}) {
  if (!settings) return null;
  const groupValues = settings[section.group] as Record<string, SettingValue>;

  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-6">
      <header>
        <h2 className="text-lg font-semibold tracking-tight">{section.title}</h2>
        {section.hint ? (
          <p className="mt-1 text-xs text-neutral-500">{section.hint}</p>
        ) : null}
      </header>

      <div className="mt-4 flex flex-col gap-4 sm:max-w-xl">
        {section.fields.map((field) => (
          <SettingField
            key={field.key}
            field={field}
            currentValue={groupValues[field.key]}
            isSaving={savingKey === field.key}
            error={errorByKey[field.key] ?? null}
            savedAt={successByKey[field.key] ?? null}
            onSave={onSave}
          />
        ))}
      </div>
    </section>
  );
}

function SettingField({
  field,
  currentValue,
  isSaving,
  error,
  savedAt,
  onSave,
}: {
  field: FieldDef;
  currentValue: SettingValue | undefined;
  isSaving: boolean;
  error: string | null;
  savedAt: number | null;
  onSave: (key: string, value: SettingValue) => void;
}) {
  const [draft, setDraft] = useState(
    currentValue === undefined ? "" : String(currentValue),
  );
  const [showSaved, setShowSaved] = useState(false);

  // Re-seed when the upstream value changes (e.g., after a successful save).
  useEffect(() => {
    if (currentValue !== undefined) setDraft(String(currentValue));
  }, [currentValue]);

  useEffect(() => {
    if (!savedAt) return;
    setShowSaved(true);
    const t = setTimeout(() => setShowSaved(false), 3000);
    return () => clearTimeout(t);
  }, [savedAt]);

  // Boolean settings save immediately on toggle — no separate submit button.
  if (field.type === "bool") {
    const checked = currentValue === true;
    return (
      <div className="flex items-center justify-between gap-3">
        <div>
          <span className="text-sm font-medium text-neutral-800">
            {field.label}
          </span>
          {field.hint ? (
            <p className="text-xs text-neutral-400">{field.hint}</p>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <label className="inline-flex cursor-pointer items-center gap-2">
            <input
              type="checkbox"
              checked={checked}
              disabled={isSaving}
              onChange={(e) => onSave(field.key, e.target.checked)}
              className="h-4 w-4"
            />
            <span className="text-sm text-neutral-700">
              {checked ? "Вкл" : "Выкл"}
            </span>
          </label>
          {showSaved ? (
            <span className="text-xs font-medium text-emerald-700">
              Сохранено
            </span>
          ) : null}
        </div>
        {error ? <p className="text-xs text-rose-600">{error}</p> : null}
      </div>
    );
  }

  if (field.type === "select") {
    return (
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr,auto] sm:items-end">
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium text-neutral-800">{field.label}</span>
          <select
            value={draft}
            disabled={isSaving}
            onChange={(e) => onSave(field.key, e.target.value)}
            className="rounded border border-neutral-300 px-3 py-2 text-sm"
          >
            {(field.options ?? []).map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          {field.hint ? (
            <span className="text-xs text-neutral-400">{field.hint}</span>
          ) : null}
        </label>
        {showSaved ? (
          <span className="text-xs font-medium text-emerald-700">Сохранено</span>
        ) : null}
        {error ? <p className="text-xs text-rose-600">{error}</p> : null}
      </div>
    );
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const num = field.type === "int" ? Number.parseInt(draft, 10) : Number(draft);
    if (!Number.isFinite(num)) return;
    onSave(field.key, num);
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr,auto,auto] sm:items-end"
      noValidate
    >
      <Input
        label={field.label}
        type="number"
        inputMode={field.type === "int" ? "numeric" : "decimal"}
        step={field.type === "int" ? 1 : 0.01}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        error={error ?? undefined}
        disabled={isSaving}
      />
      <Button type="submit" size="md" loading={isSaving} disabled={isSaving}>
        Сохранить
      </Button>
      {showSaved ? (
        <span className="text-xs font-medium text-emerald-700">Сохранено</span>
      ) : (
        <span className="text-xs text-neutral-400">{field.hint ?? ""}</span>
      )}
    </form>
  );
}

export default SettingsPage;
