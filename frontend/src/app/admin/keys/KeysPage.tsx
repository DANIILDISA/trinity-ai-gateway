"use client";

import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { formatDateTime, formatTokenCount } from "@/lib/formatting";
import type { ApiKey } from "@/types/admin";

export interface KeysPageProps {
  keys: ApiKey[];
  isLoading: boolean;
  loadError: string | null;

  addLabel: string;
  addKeyValue: string;
  addAccountName: string;
  isAdding: boolean;
  addError: string | null;
  addSuccess: string | null;

  onAddLabelChange: (value: string) => void;
  onAddKeyChange: (value: string) => void;
  onAddAccountNameChange: (value: string) => void;
  onAddSubmit: (e: React.FormEvent) => void;

  onActivate: (id: string) => void;
  onDisable: (id: string) => void;
  onEnable: (id: string) => void;
  onBlockAccount: (accountName: string) => void;
  onUnblockAccount: (accountName: string) => void;

  busyKeyId: string | null;
  busyAccount: string | null;
}

export function KeysPage({
  keys,
  isLoading,
  loadError,
  addLabel,
  addKeyValue,
  addAccountName,
  isAdding,
  addError,
  addSuccess,
  onAddLabelChange,
  onAddKeyChange,
  onAddAccountNameChange,
  onAddSubmit,
  onActivate,
  onDisable,
  onEnable,
  onBlockAccount,
  onUnblockAccount,
  busyKeyId,
  busyAccount,
}: KeysPageProps) {
  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-semibold tracking-tight">Ключи OpenRouter</h1>

      <section className="rounded-lg border border-neutral-200 bg-white p-6">
        <h2 className="text-lg font-semibold tracking-tight">Добавить ключ</h2>
        <form
          className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-[1fr,1fr,1fr,auto]"
          onSubmit={onAddSubmit}
          noValidate
        >
          <Input
            label="Метка"
            placeholder="например, primary"
            value={addLabel}
            onChange={(e) => onAddLabelChange(e.target.value)}
            disabled={isAdding}
          />
          <Input
            label="API ключ"
            type="password"
            placeholder="sk-or-v1-..."
            value={addKeyValue}
            onChange={(e) => onAddKeyChange(e.target.value)}
            disabled={isAdding}
            autoComplete="off"
          />
          <Input
            label="Аккаунт (необязательно)"
            placeholder="например, account-1"
            value={addAccountName}
            onChange={(e) => onAddAccountNameChange(e.target.value)}
            disabled={isAdding}
          />
          <div className="flex items-end">
            <Button type="submit" loading={isAdding} disabled={isAdding}>
              Добавить
            </Button>
          </div>
        </form>
        {addError ? (
          <p role="alert" className="mt-3 text-sm text-rose-600">
            {addError}
          </p>
        ) : null}
        {addSuccess ? (
          <p
            role="status"
            className="mt-3 rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800"
          >
            {addSuccess}
          </p>
        ) : null}
      </section>

      <section className="rounded-lg border border-neutral-200 bg-white">
        <header className="border-b border-neutral-200 px-4 py-3">
          <h2 className="text-base font-semibold tracking-tight">
            Список ключей
          </h2>
        </header>

        {loadError ? (
          <div className="px-4 py-3 text-sm text-rose-700">{loadError}</div>
        ) : null}

        {isLoading && keys.length === 0 ? (
          <div className="px-4 py-6 text-sm text-neutral-500">Загрузка...</div>
        ) : keys.length === 0 ? (
          <div className="px-4 py-6 text-sm text-neutral-500">
            Ключи не добавлены.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-neutral-50 text-left text-xs uppercase tracking-wide text-neutral-500">
                <tr>
                  <th className="px-4 py-2 font-medium">Метка</th>
                  <th className="px-4 py-2 font-medium">Аккаунт</th>
                  <th className="px-4 py-2 font-medium">Suffix</th>
                  <th className="px-4 py-2 font-medium">Статус</th>
                  <th className="px-4 py-2 font-medium">Primary</th>
                  <th className="px-4 py-2 text-right font-medium">Запросов</th>
                  <th className="px-4 py-2 text-right font-medium">Cost (μ$)</th>
                  <th className="px-4 py-2 font-medium">Last used</th>
                  <th className="px-4 py-2 text-right font-medium">Действия</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-neutral-100">
                {keys.map((k) => (
                  <KeyRow
                    key={k.id}
                    apiKey={k}
                    busy={busyKeyId === k.id}
                    accountBusy={
                      k.account_name !== null && busyAccount === k.account_name
                    }
                    onActivate={() => onActivate(k.id)}
                    onDisable={() => onDisable(k.id)}
                    onEnable={() => onEnable(k.id)}
                    onBlockAccount={onBlockAccount}
                    onUnblockAccount={onUnblockAccount}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const active = status === "active";
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
        active
          ? "bg-emerald-100 text-emerald-800"
          : status === "exhausted"
            ? "bg-amber-100 text-amber-800"
            : "bg-neutral-200 text-neutral-700"
      }`}
    >
      {active ? "Активен" : status === "exhausted" ? "Исчерпан" : "Отключён"}
    </span>
  );
}

function KeyRow({
  apiKey,
  busy,
  accountBusy,
  onActivate,
  onDisable,
  onEnable,
  onBlockAccount,
  onUnblockAccount,
}: {
  apiKey: ApiKey;
  busy: boolean;
  accountBusy: boolean;
  onActivate: () => void;
  onDisable: () => void;
  onEnable: () => void;
  onBlockAccount: (accountName: string) => void;
  onUnblockAccount: (accountName: string) => void;
}) {
  return (
    <tr>
      <td className="px-4 py-2">
        <div className="font-medium text-neutral-900">{apiKey.label}</div>
        <div className="text-[11px] text-neutral-500">
          Добавлен {formatDateTime(apiKey.created_at)}
        </div>
      </td>
      <td className="px-4 py-2">
        {apiKey.account_name ? (
          <div className="flex flex-col gap-1">
            <span className="text-sm text-neutral-700">
              {apiKey.account_name}
            </span>
            {apiKey.account_blocked ? (
              <span className="inline-flex w-fit items-center rounded-full bg-rose-100 px-2 py-0.5 text-xs font-medium text-rose-800">
                ⛔ Заблокирован
              </span>
            ) : null}
            <button
              type="button"
              disabled={accountBusy}
              onClick={() =>
                apiKey.account_blocked
                  ? onUnblockAccount(apiKey.account_name as string)
                  : onBlockAccount(apiKey.account_name as string)
              }
              className="w-fit text-left text-[11px] text-neutral-500 underline hover:text-neutral-800 disabled:opacity-50"
            >
              {apiKey.account_blocked
                ? "Разблокировать аккаунт"
                : "Заблокировать аккаунт"}
            </button>
          </div>
        ) : (
          <span className="text-xs text-neutral-400">—</span>
        )}
      </td>
      <td className="px-4 py-2 font-mono text-xs text-neutral-700">
        …{apiKey.key_suffix}
      </td>
      <td className="px-4 py-2">
        <StatusBadge status={apiKey.status} />
      </td>
      <td className="px-4 py-2">
        {apiKey.is_primary ? (
          <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800">
            ★ Основной
          </span>
        ) : (
          <span className="text-xs text-neutral-400">—</span>
        )}
      </td>
      <td className="px-4 py-2 text-right font-mono tabular-nums">
        {formatTokenCount(apiKey.total_requests)}
      </td>
      <td className="px-4 py-2 text-right font-mono tabular-nums">
        {formatTokenCount(apiKey.total_cost_micro_usd)}
      </td>
      <td className="px-4 py-2 text-xs text-neutral-600">
        {apiKey.last_used_at ? formatDateTime(apiKey.last_used_at) : "—"}
      </td>
      <td className="px-4 py-2 text-right">
        <div className="flex justify-end gap-2">
          {!apiKey.is_primary && apiKey.status === "active" ? (
            <Button
              size="sm"
              variant="secondary"
              disabled={busy}
              onClick={onActivate}
            >
              Сделать основным
            </Button>
          ) : null}
          {apiKey.status === "active" ? (
            <Button size="sm" variant="ghost" disabled={busy} onClick={onDisable}>
              Отключить
            </Button>
          ) : (
            <Button size="sm" variant="ghost" disabled={busy} onClick={onEnable}>
              Активировать
            </Button>
          )}
        </div>
      </td>
    </tr>
  );
}

export default KeysPage;
