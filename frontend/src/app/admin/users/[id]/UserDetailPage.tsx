"use client";

import Link from "next/link";

import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import { formatBalance, formatDateTime, formatTokenCount } from "@/lib/formatting";
import type { AdminUserDetail } from "@/types/admin";

export interface UserDetailPageProps {
  user: AdminUserDetail | null;
  isLoading: boolean;
  error: string | null;

  blockReason: string;
  isBlocking: boolean;
  blockError: string | null;
  isUnblocking: boolean;
  isShowingBlockForm: boolean;

  adjustAmount: string;
  adjustReason: string;
  isAdjusting: boolean;
  adjustError: string | null;
  adjustSuccess: string | null;

  onBlockReasonChange: (value: string) => void;
  onShowBlockForm: () => void;
  onHideBlockForm: () => void;
  onBlock: () => void;
  onUnblock: () => void;
  onAdjustAmountChange: (value: string) => void;
  onAdjustReasonChange: (value: string) => void;
  onAdjustSubmit: (e: React.FormEvent) => void;
}

export function UserDetailPage(props: UserDetailPageProps) {
  const { user, isLoading, error } = props;

  if (isLoading && !user) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <Spinner size="lg" />
      </div>
    );
  }

  if (error && !user) {
    return (
      <div className="flex flex-col gap-4">
        <Link
          href="/admin/users"
          className="text-sm text-blue-600 hover:underline"
        >
          ← К списку пользователей
        </Link>
        <p className="rounded border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
          {error}
        </p>
      </div>
    );
  }

  if (!user) return null;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <Link
          href="/admin/users"
          className="text-sm text-blue-600 hover:underline"
        >
          ← К списку пользователей
        </Link>
      </div>

      <ProfileCard user={user} />
      <StatsCard user={user} />
      <AdjustBalanceCard {...props} user={user} />
      <BlockCard {...props} user={user} />
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
          : "bg-rose-100 text-rose-800"
      }`}
    >
      {active ? "Активен" : "Заблокирован"}
    </span>
  );
}

function RoleBadge({ role }: { role: string }) {
  const isAdmin = role === "admin";
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
        isAdmin
          ? "bg-amber-100 text-amber-800"
          : "bg-neutral-100 text-neutral-700"
      }`}
    >
      {isAdmin ? "Админ" : "Пользователь"}
    </span>
  );
}

function ProfileCard({ user }: { user: AdminUserDetail }) {
  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-6">
      <header className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">
            {user.display_name}
          </h1>
          <p className="text-sm text-neutral-600">{user.email}</p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <RoleBadge role={user.role} />
          <StatusBadge status={user.status} />
        </div>
      </header>

      <dl className="mt-6 grid grid-cols-1 gap-x-6 gap-y-2 text-sm md:grid-cols-2">
        <Row label="ID" value={user.id} mono />
        <Row label="Зарегистрирован" value={formatDateTime(user.created_at)} />
        <Row
          label="Последняя активность"
          value={
            user.last_active_at ? formatDateTime(user.last_active_at) : "—"
          }
        />
        <Row
          label="Активные сессии"
          value={user.sessions_count.toString()}
        />
        <Row label="Пароль" value={user.has_password ? "Установлен" : "Нет"} />
        <Row
          label="Яндекс OAuth"
          value={user.has_yandex ? "Подключён" : "Нет"}
        />
      </dl>
    </section>
  );
}

function Row({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex flex-col">
      <dt className="text-xs uppercase tracking-wide text-neutral-500">
        {label}
      </dt>
      <dd
        className={`text-neutral-900 ${mono ? "font-mono text-xs" : "text-sm"}`}
      >
        {value}
      </dd>
    </div>
  );
}

function StatsCard({ user }: { user: AdminUserDetail }) {
  const s = user.stats;
  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-6">
      <h2 className="text-lg font-semibold tracking-tight">Статистика</h2>
      <dl className="mt-4 grid grid-cols-2 gap-4 md:grid-cols-3">
        <Metric
          label="Всего пополнено"
          value={formatBalance(s.total_topped_up_display)}
        />
        <Metric
          label="Всего потрачено"
          value={formatBalance(s.total_spent_display)}
        />
        <Metric
          label="Средний чек запроса"
          value={formatBalance(s.avg_request_cost_display)}
          hint="(последние 100)"
        />
        <Metric
          label="Всего запросов"
          value={formatTokenCount(s.total_requests)}
        />
        <Metric
          label="Запросов сегодня"
          value={formatTokenCount(s.requests_today)}
        />
        <Metric label="Токенов всего" value={formatTokenCount(s.total_tokens)} />
        <Metric
          label="Чаще всего используется"
          value={s.most_used_model ?? "—"}
          wide
        />
      </dl>
    </section>
  );
}

function Metric({
  label,
  value,
  hint,
  wide = false,
}: {
  label: string;
  value: string;
  hint?: string;
  wide?: boolean;
}) {
  return (
    <div className={wide ? "col-span-2 md:col-span-3" : ""}>
      <p className="text-xs uppercase tracking-wide text-neutral-500">
        {label} {hint ? <span className="normal-case text-neutral-400">{hint}</span> : null}
      </p>
      <p className="mt-1 truncate text-base font-semibold text-neutral-900">
        {value}
      </p>
    </div>
  );
}

function AdjustBalanceCard({
  user,
  adjustAmount,
  adjustReason,
  isAdjusting,
  adjustError,
  adjustSuccess,
  onAdjustAmountChange,
  onAdjustReasonChange,
  onAdjustSubmit,
}: UserDetailPageProps & { user: AdminUserDetail }) {
  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-6">
      <h2 className="text-lg font-semibold tracking-tight">
        Корректировка баланса
      </h2>
      <p className="mt-1 text-sm text-neutral-600">
        Текущий баланс:{" "}
        <span className="font-medium text-neutral-900">
          {formatBalance(user.balance)}
        </span>
      </p>

      <form
        className="mt-4 flex flex-col gap-3 sm:max-w-md"
        onSubmit={onAdjustSubmit}
        noValidate
      >
        <Input
          label="Сумма (₽, со знаком: +кредит, −дебет)"
          type="number"
          inputMode="numeric"
          step={1}
          value={adjustAmount}
          onChange={(e) => onAdjustAmountChange(e.target.value)}
          disabled={isAdjusting}
        />
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-neutral-700">Причина</span>
          <textarea
            value={adjustReason}
            onChange={(e) => onAdjustReasonChange(e.target.value)}
            disabled={isAdjusting}
            placeholder="Причина корректировки..."
            rows={3}
            className="rounded border border-neutral-300 px-3 py-2 text-sm outline-none focus:border-neutral-900 disabled:bg-neutral-100"
          />
        </label>

        {adjustError ? (
          <p role="alert" className="text-sm text-rose-600">
            {adjustError}
          </p>
        ) : null}
        {adjustSuccess ? (
          <p
            role="status"
            className="rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800"
          >
            {adjustSuccess}
          </p>
        ) : null}

        <Button type="submit" loading={isAdjusting} disabled={isAdjusting}>
          Применить
        </Button>
      </form>
    </section>
  );
}

function BlockCard({
  user,
  blockReason,
  isBlocking,
  blockError,
  isUnblocking,
  isShowingBlockForm,
  onBlockReasonChange,
  onShowBlockForm,
  onHideBlockForm,
  onBlock,
  onUnblock,
}: UserDetailPageProps & { user: AdminUserDetail }) {
  if (user.status === "blocked") {
    return (
      <section className="rounded-lg border border-rose-200 bg-rose-50 p-6">
        <h2 className="text-lg font-semibold tracking-tight text-rose-900">
          Аккаунт заблокирован
        </h2>
        {user.block_reason ? (
          <p className="mt-2 text-sm text-rose-800">
            Причина: {user.block_reason}
          </p>
        ) : null}
        {blockError ? (
          <p role="alert" className="mt-2 text-sm text-rose-700">
            {blockError}
          </p>
        ) : null}
        <Button
          variant="primary"
          className="mt-4"
          loading={isUnblocking}
          disabled={isUnblocking}
          onClick={onUnblock}
        >
          Разблокировать
        </Button>
      </section>
    );
  }

  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-6">
      <h2 className="text-lg font-semibold tracking-tight">Блокировка</h2>
      <p className="mt-1 text-sm text-neutral-600">
        Заблокированный пользователь не сможет отправлять запросы и пополнять баланс.
      </p>

      {!isShowingBlockForm ? (
        <Button
          variant="primary"
          className="mt-4 bg-rose-600 hover:bg-rose-700"
          onClick={onShowBlockForm}
        >
          Заблокировать
        </Button>
      ) : (
        <div className="mt-4 flex flex-col gap-3 sm:max-w-md">
          <label className="flex flex-col gap-1">
            <span className="text-sm font-medium text-neutral-700">
              Причина блокировки
            </span>
            <textarea
              value={blockReason}
              onChange={(e) => onBlockReasonChange(e.target.value)}
              disabled={isBlocking}
              rows={3}
              autoFocus
              className="rounded border border-neutral-300 px-3 py-2 text-sm outline-none focus:border-neutral-900 disabled:bg-neutral-100"
            />
          </label>
          {blockError ? (
            <p role="alert" className="text-sm text-rose-600">
              {blockError}
            </p>
          ) : null}
          <div className="flex gap-2">
            <Button
              className="bg-rose-600 hover:bg-rose-700"
              loading={isBlocking}
              disabled={isBlocking || blockReason.trim().length === 0}
              onClick={onBlock}
            >
              Подтвердить блокировку
            </Button>
            <Button
              variant="secondary"
              disabled={isBlocking}
              onClick={onHideBlockForm}
            >
              Отмена
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}

export default UserDetailPage;
