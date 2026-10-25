"use client";

import { useState } from "react";

import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import { formatDateTime } from "@/lib/formatting";
import type { SessionItem, User } from "@/types/api";
import {
  AccentButton,
  EmptyState,
  GhostButton,
  Panel,
  SectionShell,
} from "./primitives";

export interface AccountSectionProps {
  user: User | null;
  sessions: SessionItem[];
  isLoadingSessions: boolean;
  onTerminateSession: (id: string) => void;
  onTerminateAllSessions: () => void;
  deleteEmailInput: string;
  onDeleteEmailInputChange: (value: string) => void;
  isDeletingAccount: boolean;
  deleteError: string | null;
  onConfirmDeleteAccount: () => void;
}

export function AccountSection(props: AccountSectionProps) {
  return (
    <SectionShell
      title="Аккаунт"
      description="Сессии, выход и удаление аккаунта."
    >
      <Panel title="Выход">
        <p className="mb-3 text-sm text-neutral-400">
          Завершит все активные сессии, включая текущую.
        </p>
        <GhostButton onClick={props.onTerminateAllSessions}>
          Выйти со всех устройств
        </GhostButton>
      </Panel>

      <SessionsPanel
        sessions={props.sessions}
        isLoading={props.isLoadingSessions}
        onTerminate={props.onTerminateSession}
      />

      <DeleteAccountPanel
        userEmail={props.user?.email ?? ""}
        deleteEmailInput={props.deleteEmailInput}
        onDeleteEmailInputChange={props.onDeleteEmailInputChange}
        isDeleting={props.isDeletingAccount}
        deleteError={props.deleteError}
        onConfirm={props.onConfirmDeleteAccount}
      />
    </SectionShell>
  );
}

function SessionsPanel({
  sessions,
  isLoading,
  onTerminate,
}: {
  sessions: SessionItem[];
  isLoading: boolean;
  onTerminate: (id: string) => void;
}) {
  return (
    <Panel title="Активные сессии">
      {isLoading && sessions.length === 0 ? (
        <div className="flex justify-center py-4">
          <Spinner />
        </div>
      ) : sessions.length === 0 ? (
        <EmptyState>Нет активных сессий</EmptyState>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-left text-sm">
            <thead>
              <tr className="border-b border-neutral-800 font-mono text-[10px] uppercase tracking-wider text-neutral-600">
                <th className="py-2 pr-3 font-medium">Устройство</th>
                <th className="py-2 pr-3 font-medium">Местоположение</th>
                <th className="py-2 pr-3 font-medium">Создана</th>
                <th className="py-2 pr-3 font-medium">Истекает</th>
                <th className="py-2 pr-3 font-medium" aria-label="Действия" />
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-800/60">
              {sessions.map((s) => (
                <SessionRow key={s.id} session={s} onTerminate={onTerminate} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

function SessionRow({
  session,
  onTerminate,
}: {
  session: SessionItem;
  onTerminate: (id: string) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const device = shortDevice(session.user_agent);

  return (
    <tr className="text-neutral-300">
      <td className="py-3 pr-3 align-top">
        <div className="flex items-center gap-2">
          <span className="truncate">{device}</span>
          {session.is_current ? (
            <span className="inline-flex items-center rounded-md border border-emerald-600/40 bg-emerald-600/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider text-emerald-300">
              Текущая
            </span>
          ) : null}
        </div>
      </td>
      <td className="py-3 pr-3 align-top font-mono text-xs tabular-nums text-neutral-400">
        {session.ip_address ?? "—"}
      </td>
      <td className="py-3 pr-3 align-top font-mono text-[11px] tabular-nums text-neutral-500">
        {formatDateTime(session.created_at)}
      </td>
      <td className="py-3 pr-3 align-top font-mono text-[11px] tabular-nums text-neutral-500">
        {formatDateTime(session.expires_at)}
      </td>
      <td className="relative py-3 pr-3 align-top text-right">
        <button
          type="button"
          onClick={() => setMenuOpen((v) => !v)}
          aria-label="Действия"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          className="inline-flex h-7 w-7 items-center justify-center rounded-md text-neutral-400 transition-colors hover:bg-neutral-800 hover:text-neutral-200"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <circle cx="5" cy="12" r="1.6" />
            <circle cx="12" cy="12" r="1.6" />
            <circle cx="19" cy="12" r="1.6" />
          </svg>
        </button>
        {menuOpen ? (
          <>
            <button
              type="button"
              aria-hidden="true"
              tabIndex={-1}
              onClick={() => setMenuOpen(false)}
              className="fixed inset-0 z-30 cursor-default"
            />
            <div
              role="menu"
              className="absolute right-2 top-9 z-40 w-44 overflow-hidden rounded-lg border border-neutral-700 bg-neutral-800 py-1 text-left shadow-xl"
            >
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false);
                  onTerminate(session.id);
                }}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-sm text-red-400 transition-colors hover:bg-red-900/30"
              >
                Завершить сессию
              </button>
            </div>
          </>
        ) : null}
      </td>
    </tr>
  );
}

// Trim the User-Agent down to a recognizable phrase. Full UAs are noisy
// (browser version + OS build + every plugin); we want "Chrome / Windows".
function shortDevice(ua: string | null): string {
  if (!ua) return "Неизвестно";
  const lower = ua.toLowerCase();
  let browser = "Браузер";
  if (lower.includes("yabrowser")) browser = "Яндекс.Браузер";
  else if (lower.includes("edg/")) browser = "Edge";
  else if (lower.includes("chrome/")) browser = "Chrome";
  else if (lower.includes("firefox/")) browser = "Firefox";
  else if (lower.includes("safari/")) browser = "Safari";

  let os = "";
  if (lower.includes("windows")) os = "Windows";
  else if (lower.includes("mac os") || lower.includes("macos")) os = "macOS";
  else if (lower.includes("android")) os = "Android";
  else if (lower.includes("iphone") || lower.includes("ios")) os = "iOS";
  else if (lower.includes("linux")) os = "Linux";

  return os ? `${browser} · ${os}` : browser;
}

function DeleteAccountPanel({
  userEmail,
  deleteEmailInput,
  onDeleteEmailInputChange,
  isDeleting,
  deleteError,
  onConfirm,
}: {
  userEmail: string;
  deleteEmailInput: string;
  onDeleteEmailInputChange: (value: string) => void;
  isDeleting: boolean;
  deleteError: string | null;
  onConfirm: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const enabled = deleteEmailInput.trim().toLowerCase() === userEmail.toLowerCase();

  return (
    <Panel title="Удаление аккаунта">
      <p className="mb-3 text-sm text-neutral-400">
        Аккаунт будет помечен на удаление. Чтобы восстановить доступ,
        войдите снова со своим паролем — мы предложим восстановление.
      </p>
      {!confirming ? (
        <button
          type="button"
          onClick={() => setConfirming(true)}
          className="inline-flex h-10 items-center justify-center gap-2 rounded-md border border-red-800/60 bg-red-950/30 px-5 text-sm font-medium text-red-300 transition-colors hover:border-red-700 hover:bg-red-900/40 hover:text-red-200"
        >
          Удалить аккаунт
        </button>
      ) : (
        <div className="flex flex-col gap-3 rounded-md border border-red-900/40 bg-red-950/20 p-4">
          <p className="text-sm text-neutral-300">
            Подтвердите удаление, введя ваш email:{" "}
            <span className="font-mono text-neutral-100">{userEmail}</span>
          </p>
          <Input
            label="Введите email для подтверждения"
            type="email"
            autoComplete="off"
            value={deleteEmailInput}
            onChange={(e) => onDeleteEmailInputChange(e.target.value)}
          />
          {deleteError ? (
            <p role="alert" className="text-xs text-red-400">
              {deleteError}
            </p>
          ) : null}
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={onConfirm}
              disabled={!enabled || isDeleting}
              className="inline-flex h-10 items-center justify-center gap-2 rounded-md bg-red-600 px-5 text-sm font-medium text-white transition-colors hover:bg-red-500 disabled:cursor-not-allowed disabled:bg-red-600/30 disabled:text-white/50"
            >
              {isDeleting ? <Spinner size="sm" /> : null}
              Подтвердить удаление
            </button>
            <button
              type="button"
              onClick={() => {
                setConfirming(false);
                onDeleteEmailInputChange("");
              }}
              disabled={isDeleting}
              className="inline-flex h-10 items-center justify-center gap-2 rounded-md border border-neutral-700 bg-neutral-800 px-4 text-sm font-medium text-neutral-200 transition-colors hover:bg-neutral-700 disabled:opacity-50"
            >
              Отмена
            </button>
          </div>
        </div>
      )}
    </Panel>
  );
}

// AccentButton import kept so the export shape stays self-explanatory even
// if it's not used directly in this file yet.
void AccentButton;
