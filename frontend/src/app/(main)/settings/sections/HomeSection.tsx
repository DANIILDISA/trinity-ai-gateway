"use client";

import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import { formatRelativeDate } from "@/lib/formatting";
import type { Notification, User } from "@/types/api";
import type { AppearanceMode } from "../SettingsPage";
import {
  AccentButton,
  EmptyState,
  Panel,
  SectionShell,
} from "./primitives";

export interface HomeSectionProps {
  // Profile
  user: User | null;
  displayName: string;
  displayNameError: string | null;
  isSavingDisplayName: boolean;
  displayNameSavedAt: number | null;
  onDisplayNameChange: (value: string) => void;
  onSaveDisplayName: () => void;
  fullName: string;
  fullNameError: string | null;
  isSavingFullName: boolean;
  fullNameSavedAt: number | null;
  onFullNameChange: (value: string) => void;
  onSaveFullName: () => void;

  // Appearance
  appearance: AppearanceMode;
  onAppearanceChange: (mode: AppearanceMode) => void;

  // Notifications
  notifications: Notification[];
  isLoadingNotifications: boolean;
  notificationPrefs: Record<string, boolean>;
  onToggleNotificationPref: (key: string) => void;
  onMarkAsRead: (id: string) => void;

  // Password (Yandex-only accounts)
  showPasswordSection: boolean;
  newPassword: string;
  confirmPassword: string;
  passwordError: string | null;
  isSavingPassword: boolean;
  passwordSuccess: boolean;
  onNewPasswordChange: (value: string) => void;
  onConfirmPasswordChange: (value: string) => void;
  onSetPassword: () => void;
}

export function HomeSection(props: HomeSectionProps) {
  return (
    <SectionShell title="Главная" description="Профиль и базовые предпочтения.">
      <ProfilePanel
        user={props.user}
        displayName={props.displayName}
        displayNameError={props.displayNameError}
        isSavingDisplayName={props.isSavingDisplayName}
        displayNameSavedAt={props.displayNameSavedAt}
        onDisplayNameChange={props.onDisplayNameChange}
        onSaveDisplayName={props.onSaveDisplayName}
        fullName={props.fullName}
        fullNameError={props.fullNameError}
        isSavingFullName={props.isSavingFullName}
        fullNameSavedAt={props.fullNameSavedAt}
        onFullNameChange={props.onFullNameChange}
        onSaveFullName={props.onSaveFullName}
      />
      <AppearancePanel
        value={props.appearance}
        onChange={props.onAppearanceChange}
      />
      <NotificationsPanel
        notifications={props.notifications}
        isLoading={props.isLoadingNotifications}
        prefs={props.notificationPrefs}
        onTogglePref={props.onToggleNotificationPref}
        onMarkAsRead={props.onMarkAsRead}
      />
      {props.showPasswordSection ? (
        <PasswordPanel
          newPassword={props.newPassword}
          confirmPassword={props.confirmPassword}
          passwordError={props.passwordError}
          isSavingPassword={props.isSavingPassword}
          passwordSuccess={props.passwordSuccess}
          onNewPasswordChange={props.onNewPasswordChange}
          onConfirmPasswordChange={props.onConfirmPasswordChange}
          onSetPassword={props.onSetPassword}
        />
      ) : null}
    </SectionShell>
  );
}

function ProfilePanel({
  user,
  displayName,
  displayNameError,
  isSavingDisplayName,
  displayNameSavedAt,
  onDisplayNameChange,
  onSaveDisplayName,
  fullName,
  fullNameError,
  isSavingFullName,
  fullNameSavedAt,
  onFullNameChange,
  onSaveFullName,
}: Pick<
  HomeSectionProps,
  | "user"
  | "displayName"
  | "displayNameError"
  | "isSavingDisplayName"
  | "displayNameSavedAt"
  | "onDisplayNameChange"
  | "onSaveDisplayName"
  | "fullName"
  | "fullNameError"
  | "isSavingFullName"
  | "fullNameSavedAt"
  | "onFullNameChange"
  | "onSaveFullName"
>) {
  // tr-uxkp: the avatar must reflect the SAVED profile, not the in-progress
  // input — otherwise typing a name (or a save that 422s on an over-limit value)
  // changes the avatar as if it were saved. Derive it from the persisted user.
  const initials = computeInitials(
    user?.full_name || user?.display_name || user?.email || "?",
  );
  const displayNameSaved =
    displayNameSavedAt !== null && Date.now() - displayNameSavedAt < 5_000;
  const fullNameSaved =
    fullNameSavedAt !== null && Date.now() - fullNameSavedAt < 5_000;

  return (
    <Panel title="Профиль">
      <div className="flex flex-col gap-6 sm:flex-row sm:items-start">
        <div className="flex shrink-0 flex-col items-center gap-2">
          <div className="relative">
            <div className="flex h-20 w-20 items-center justify-center rounded-full bg-emerald-600/15 text-2xl font-semibold text-emerald-300">
              {initials}
            </div>
          </div>
          <p className="text-center font-mono text-[10px] uppercase tracking-wider text-neutral-600">
            Загрузка фото<br />появится позже
          </p>
        </div>

        <div className="flex min-w-0 flex-1 flex-col gap-4">
          <div className="grid grid-cols-[6rem,1fr] items-center gap-3 border-b border-neutral-800/60 pb-3">
            <span className="font-mono text-[11px] uppercase tracking-wider text-neutral-500">
              Email
            </span>
            <span className="truncate font-mono text-sm text-neutral-200">
              {user?.email ?? "—"}
            </span>
          </div>

          <form
            className="flex flex-col gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              onSaveFullName();
            }}
            noValidate
          >
            <Input
              label="Полное имя"
              type="text"
              autoComplete="name"
              placeholder="—"
              value={fullName}
              onChange={(e) => onFullNameChange(e.target.value)}
              error={fullNameError ?? undefined}
            />
            <div className="flex items-center gap-3">
              <AccentButton
                type="submit"
                loading={isSavingFullName}
                disabled={isSavingFullName}
              >
                Сохранить
              </AccentButton>
              {fullNameSaved ? (
                <span className="font-mono text-[11px] uppercase tracking-wider text-emerald-400">
                  ✓ Сохранено
                </span>
              ) : null}
            </div>
          </form>

          <form
            className="flex flex-col gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              onSaveDisplayName();
            }}
            noValidate
          >
            <Input
              label="Как к вам обращаться?"
              type="text"
              autoComplete="nickname"
              value={displayName}
              onChange={(e) => onDisplayNameChange(e.target.value)}
              error={displayNameError ?? undefined}
            />
            <div className="flex items-center gap-3">
              <AccentButton
                type="submit"
                loading={isSavingDisplayName}
                disabled={isSavingDisplayName}
              >
                Сохранить
              </AccentButton>
              {displayNameSaved ? (
                <span className="font-mono text-[11px] uppercase tracking-wider text-emerald-400">
                  ✓ Сохранено
                </span>
              ) : null}
            </div>
          </form>
        </div>
      </div>
    </Panel>
  );
}

function computeInitials(source: string): string {
  const trimmed = source.trim();
  if (!trimmed) return "?";
  const parts = trimmed.split(/\s+/).filter(Boolean);
  if (parts.length === 1) {
    return parts[0].slice(0, 2).toUpperCase();
  }
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

const APPEARANCE_OPTIONS: { value: AppearanceMode; label: string }[] = [
  { value: "system", label: "Системная" },
  { value: "light", label: "Светлая" },
  { value: "dark", label: "Тёмная" },
];

function AppearancePanel({
  value,
  onChange,
}: {
  value: AppearanceMode;
  onChange: (mode: AppearanceMode) => void;
}) {
  // Indicator position is driven by index — Tailwind's translate utilities
  // give us a smooth slide via transition-transform. Each cell is 1/3 of
  // the track width; the indicator absolute-positioned to slide between them.
  const activeIndex = APPEARANCE_OPTIONS.findIndex((o) => o.value === value);
  const safeIndex = activeIndex >= 0 ? activeIndex : 0;

  return (
    <Panel title="Внешний вид">
      <p className="mb-3 text-sm text-neutral-400">
        Сейчас приложение всегда использует тёмную тему. Переключатель — для
        будущих режимов.
      </p>
      <div className="relative inline-flex w-full max-w-md rounded-lg border border-neutral-700 bg-neutral-900 p-1">
        <span
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-1 left-1 w-[calc((100%-0.5rem)/3)] rounded-md bg-emerald-600 shadow-sm transition-transform duration-200 ease-out"
          style={{ transform: `translateX(${safeIndex * 100}%)` }}
        />
        {APPEARANCE_OPTIONS.map((opt) => {
          const active = opt.value === value;
          return (
            <button
              key={opt.value}
              type="button"
              onClick={() => onChange(opt.value)}
              className={`relative z-10 flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                active ? "text-white" : "text-neutral-400 hover:text-neutral-200"
              }`}
              aria-pressed={active}
            >
              {opt.label}
            </button>
          );
        })}
      </div>
    </Panel>
  );
}

interface NotificationPrefDescriptor {
  key: string;
  label: string;
  description: string;
}

// Prefs are local-only for now (no backend). The "Завершение ответа" pref maps
// to the existing browser-notification path; the others are visual stubs that
// stay in localStorage via the parent route's state.
const NOTIFICATION_PREFS: readonly NotificationPrefDescriptor[] = [
  {
    key: "response_done",
    label: "Завершение ответа",
    description: "Уведомлять, когда модель закончит ответ.",
  },
  {
    key: "balance_low",
    label: "Низкий баланс",
    description: "Предупреждать, когда баланс становится критически низким.",
  },
  {
    key: "system_news",
    label: "Системные новости",
    description: "Объявления о новых моделях и обновлениях.",
  },
];

function NotificationsPanel({
  notifications,
  isLoading,
  prefs,
  onTogglePref,
  onMarkAsRead,
}: {
  notifications: Notification[];
  isLoading: boolean;
  prefs: Record<string, boolean>;
  onTogglePref: (key: string) => void;
  onMarkAsRead: (id: string) => void;
}) {
  return (
    <Panel title="Уведомления">
      <ul className="flex flex-col gap-3">
        {NOTIFICATION_PREFS.map((pref) => {
          const enabled = prefs[pref.key] ?? true;
          return (
            <li
              key={pref.key}
              className="flex items-start justify-between gap-3 rounded-md border border-neutral-800 bg-neutral-900/60 px-4 py-3"
            >
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-neutral-200">
                  {pref.label}
                </p>
                <p className="text-xs text-neutral-500">{pref.description}</p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={enabled}
                onClick={() => onTogglePref(pref.key)}
                className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${
                  enabled ? "bg-emerald-600" : "bg-neutral-700"
                }`}
              >
                <span
                  aria-hidden="true"
                  className={`absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${
                    enabled ? "translate-x-5" : "translate-x-0"
                  }`}
                />
              </button>
            </li>
          );
        })}
      </ul>

      <div className="mt-5 border-t border-neutral-800 pt-4">
        <h4 className="mb-3 text-xs font-semibold uppercase tracking-wider text-neutral-500">
          Лента уведомлений
        </h4>
        {isLoading && notifications.length === 0 ? (
          <div className="flex justify-center py-4">
            <Spinner />
          </div>
        ) : notifications.length === 0 ? (
          <EmptyState>Нет уведомлений</EmptyState>
        ) : (
          <ul className="overflow-hidden rounded-md border border-neutral-800 divide-y divide-neutral-800/60">
            {notifications.slice(0, 10).map((n) => (
              <NotificationRow
                key={n.id}
                notification={n}
                onMarkAsRead={() => onMarkAsRead(n.id)}
              />
            ))}
          </ul>
        )}
      </div>
    </Panel>
  );
}

function NotificationRow({
  notification,
  onMarkAsRead,
}: {
  notification: Notification;
  onMarkAsRead: () => void;
}) {
  const unread = !notification.is_read;
  return (
    <li>
      <button
        type="button"
        onClick={onMarkAsRead}
        disabled={!unread}
        className={`flex w-full flex-col items-start gap-1 px-4 py-3 text-left transition-colors ${
          unread
            ? "bg-amber-950/30 hover:bg-amber-950/50"
            : "cursor-default bg-transparent hover:bg-neutral-800/30"
        }`}
      >
        <div className="flex w-full items-start justify-between gap-2">
          <span
            className={`text-sm ${
              unread
                ? "font-semibold text-neutral-100"
                : "font-medium text-neutral-300"
            }`}
          >
            {notification.title}
          </span>
          <span className="shrink-0 font-mono text-[11px] tabular-nums text-neutral-500">
            {formatRelativeDate(notification.created_at)}
          </span>
        </div>
        <p className="text-sm text-neutral-400">{notification.message}</p>
      </button>
    </li>
  );
}

function PasswordPanel({
  newPassword,
  confirmPassword,
  passwordError,
  isSavingPassword,
  passwordSuccess,
  onNewPasswordChange,
  onConfirmPasswordChange,
  onSetPassword,
}: Pick<
  HomeSectionProps,
  | "newPassword"
  | "confirmPassword"
  | "passwordError"
  | "isSavingPassword"
  | "passwordSuccess"
  | "onNewPasswordChange"
  | "onConfirmPasswordChange"
  | "onSetPassword"
>) {
  return (
    <Panel title="Установить пароль">
      <p className="mb-4 text-sm text-neutral-400">
        Вы вошли через Яндекс. Установите пароль для входа по email.
      </p>
      <form
        className="flex max-w-md flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          onSetPassword();
        }}
        noValidate
      >
        <Input
          label="Новый пароль"
          type="password"
          autoComplete="new-password"
          required
          value={newPassword}
          onChange={(e) => onNewPasswordChange(e.target.value)}
        />
        <Input
          label="Подтвердите пароль"
          type="password"
          autoComplete="new-password"
          required
          value={confirmPassword}
          onChange={(e) => onConfirmPasswordChange(e.target.value)}
          error={passwordError ?? undefined}
        />
        {passwordSuccess ? (
          <p
            role="status"
            className="rounded-md border border-emerald-800/50 bg-emerald-950/40 px-3 py-2 font-mono text-xs uppercase tracking-wider text-emerald-300"
          >
            ✓ Пароль успешно установлен
          </p>
        ) : null}
        <div>
          <AccentButton
            type="submit"
            loading={isSavingPassword}
            disabled={isSavingPassword}
          >
            Установить пароль
          </AccentButton>
        </div>
      </form>
    </Panel>
  );
}
