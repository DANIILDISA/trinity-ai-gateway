"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";

import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { NeuralIcon } from "@/components/ui/NeuralIcon";
import { AnimatedBalance } from "./AnimatedBalance";
import { formatBalance, formatModelName } from "@/lib/formatting";
import { selectRecents } from "@/lib/chatList";
import type { ConversationSummary } from "@/hooks/useConversations";

export interface ChatSidebarProps {
  conversations: ConversationSummary[];
  isLoading: boolean;
  currentConversationId: string | null;
  isOpen: boolean;
  userName: string | null;
  userEmail: string | null;
  // Formatted balance (e.g. "10.86 ₽") so the UserMenu can surface it proactively
  // and warn on low/zero — otherwise the balance is invisible until a failed send
  // (tr-r1n5).
  userBalance?: string | null;
  // tr-tl3q: held funds + spendable balance, to explain a low "available".
  userReserved?: string | null;
  userAvailable?: string | null;
  pinnedIds: Set<string>;
  onToggle: () => void;
  onNewChat: () => void;
  onTogglePin: (id: string) => void;
  onDelete: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onLogout: () => void;
  archived: ConversationSummary[];
  isLoadingArchived: boolean;
  onLoadArchived: () => void;
  onRestore: (id: string) => void;
  onPermanentDelete: (id: string) => void;
  onOpenAll: () => void;
}

function PlusIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

function ImageIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="3" y="3" width="18" height="18" rx="2" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="8.5" cy="8.5" r="1.5" fill="currentColor" />
      <path d="M21 15l-5-5L5 21" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

function DotsIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <circle cx="5" cy="12" r="1.6" />
      <circle cx="12" cy="12" r="1.6" />
      <circle cx="19" cy="12" r="1.6" />
    </svg>
  );
}

function PinIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M9 4h6l-1 6 3 3v2H7v-2l3-3-1-6zM12 15v5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function PencilIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M4 20h4l10-10-4-4L4 16v4zM14 6l4 4"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// Restore-from-archive: an undo arrow curving back up out of the trash. Matches
// the stroke style of the other sidebar glyphs; inherits currentColor so it
// picks up the button's neutral/hover tint.
function RestoreIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M5 9h10a5 5 0 0 1 0 10h-4M5 9l4-4M5 9l4 4"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function ChatSidebar({
  conversations,
  isLoading,
  currentConversationId,
  isOpen,
  userName,
  userEmail,
  userBalance,
  userReserved,
  userAvailable,
  pinnedIds,
  onToggle,
  onNewChat,
  onTogglePin,
  onDelete,
  onRename,
  onLogout,
  archived,
  isLoadingArchived,
  onLoadArchived,
  onRestore,
  onPermanentDelete,
  onOpenAll,
}: ChatSidebarProps) {
  // Wire the "Новый чат" keyboard shortcut (Ctrl+Shift+O) for real. A single
  // window-level listener; preventDefault to stop the browser default, then
  // start a fresh chat. Cleaned up on unmount.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (
        e.ctrlKey &&
        e.shiftKey &&
        (e.key === "O" || e.key === "o" || e.code === "KeyO")
      ) {
        e.preventDefault();
        onNewChat();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onNewChat]);

  const pinned = conversations.filter((c) => pinnedIds.has(c.id));
  const recents = selectRecents(conversations, pinnedIds);

  return (
    // The <aside> is the clipping element: it animates its width (and opacity)
    // between the full panel width and zero so BOTH opening and closing glide.
    // The inner content keeps a FIXED width and slides under the clip — without
    // this it would reflow/squish mid-animation.
    <aside
      className={`h-full shrink-0 overflow-hidden duration-300 ease-out fixed inset-y-0 left-0 z-50 w-[299px] transition-transform md:relative md:z-auto md:transition-[width,opacity] ${
        isOpen
          ? "translate-x-0 md:w-[299px] md:opacity-100"
          : "-translate-x-full md:translate-x-0 md:w-0 md:opacity-0"
      }`}
      aria-hidden={isOpen ? undefined : true}
    >
      <div
        className={`flex h-full w-[299px] shrink-0 flex-col p-2 ${
          isOpen ? "" : "pointer-events-none"
        }`}
      >
        {/* Floating "card" panel — mirrors the analytics panel: rounded container,
            subtle border, own distinct background, sitting on the canvas. */}
        <div className="group flex h-full min-h-0 flex-col overflow-hidden rounded-lg border border-neutral-800 bg-neutral-800">
      {/* Top controls */}
      <div className="flex flex-col gap-2 p-3">
        <div className="flex items-center justify-between">
          <span className="flex items-center gap-2 pl-1 text-sm font-semibold tracking-tight text-neutral-100">
            <NeuralIcon size={24} />
            Trinity
          </span>
          <button
            type="button"
            onClick={onToggle}
            title="Свернуть панель"
            aria-label="Свернуть панель"
            className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-neutral-500 transition-colors hover:bg-neutral-700/60 hover:text-neutral-200 active:scale-[0.97]"
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <rect x="3" y="4" width="18" height="16" rx="2" stroke="currentColor" strokeWidth="1.5" />
              <path d="M9 4v16" stroke="currentColor" strokeWidth="1.5" />
            </svg>
          </button>
        </div>

        <button
          type="button"
          onClick={onNewChat}
          className="group flex w-full items-center justify-between rounded-lg px-2 py-2 transition-colors hover:bg-neutral-700 active:scale-[0.97]"
        >
          <span className="flex items-center gap-2.5">
            <span className="flex h-6 w-6 items-center justify-center rounded-full border border-neutral-600 text-neutral-100">
              <PlusIcon />
            </span>
            <span className="text-sm font-medium text-neutral-100">Новый чат</span>
          </span>
          <span className="text-xs text-neutral-500 opacity-0 transition-opacity group-hover:opacity-100">
            Ctrl+Shift+O
          </span>
        </button>
        <Link
          href="/chat/gallery"
          className="flex w-full items-center gap-2.5 rounded-lg px-2 py-2 text-sm font-medium text-neutral-400 transition-colors hover:bg-neutral-700/60 hover:text-neutral-200"
        >
          <span className="flex h-6 w-6 items-center justify-center text-neutral-400">
            <ImageIcon />
          </span>
          <span>Файлы</span>
        </Link>
      </div>

      {/* Conversation list */}
      <div className="flex-1 overflow-y-auto px-2 pb-2">
        {isLoading && conversations.length === 0 ? (
          <ul className="flex flex-col gap-1 pt-2">
            {Array.from({ length: 4 }).map((_, i) => (
              <li key={i} className="h-9 animate-pulse rounded-lg bg-neutral-900" />
            ))}
          </ul>
        ) : conversations.length === 0 ? (
          <p className="px-3 py-4 text-xs text-neutral-600">Нет диалогов.</p>
        ) : (
          <>
            <Section label="Закреплённые" items={pinned}>
              {pinned.map((c) => (
                <SidebarRow
                  key={c.id}
                  conversation={c}
                  isActive={c.id === currentConversationId}
                  isPinned
                  onTogglePin={() => onTogglePin(c.id)}
                  onDelete={() => onDelete(c.id)}
                  onRename={(title) => onRename(c.id, title)}
                />
              ))}
            </Section>
            <Section
              label="Недавние"
              items={recents}
              headerAction={
                <button
                  type="button"
                  onClick={onOpenAll}
                  className="flex items-center gap-0.5 rounded text-[11px] font-medium text-neutral-400 opacity-0 transition-opacity duration-150 hover:text-neutral-100 focus-visible:opacity-100 group-hover:opacity-100"
                >
                  Показать все
                  <span aria-hidden="true">›</span>
                </button>
              }
            >
              {recents.map((c) => (
                <SidebarRow
                  key={c.id}
                  conversation={c}
                  isActive={c.id === currentConversationId}
                  isPinned={false}
                  onTogglePin={() => onTogglePin(c.id)}
                  onDelete={() => onDelete(c.id)}
                  onRename={(title) => onRename(c.id, title)}
                />
              ))}
            </Section>
          </>
        )}
      </div>

      <ArchiveSection
        archived={archived}
        isLoading={isLoadingArchived}
        onLoad={onLoadArchived}
        onRestore={onRestore}
        onPermanentDelete={onPermanentDelete}
      />

      <UserMenu
        userName={userName}
        userEmail={userEmail}
        userBalance={userBalance ?? null}
        userReserved={userReserved ?? null}
        userAvailable={userAvailable ?? null}
        onLogout={onLogout}
      />
        </div>
      </div>
    </aside>
  );
}

function SettingsGearIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="1.5" />
      <path
        d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function LogoutIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// tr-r1n5: "running low" cushion (a few requests' worth) below which the balance
// is flagged amber; <= 0 is flagged red. A heuristic, not a hard gate.
const LOW_BALANCE_RUB = 10;

function UserMenu({
  userName,
  userEmail,
  userBalance,
  userReserved,
  userAvailable,
  onLogout,
}: {
  userName: string | null;
  userEmail: string | null;
  userBalance: string | null;
  userReserved: string | null;
  userAvailable: string | null;
  onLogout: () => void;
}) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);

  // tr-r1n5: surface the balance proactively + flag low/zero so the user isn't
  // blindsided by an insufficient-funds error mid-send. Parsing failure degrades
  // gracefully — the balance string still shows, just without a tone/warning.
  const balanceNum =
    userBalance != null
      ? Number.parseFloat(userBalance.replace(/[^\d.-]/g, ""))
      : NaN;
  const balanceKnown = Number.isFinite(balanceNum);
  const isZeroBalance = balanceKnown && balanceNum <= 0;
  const isLowBalance = balanceKnown && balanceNum > 0 && balanceNum < LOW_BALANCE_RUB;
  const balanceTone = isZeroBalance
    ? "text-red-400"
    : isLowBalance
      ? "text-amber-400"
      : "text-emerald-400";
  // tr-tl3q: when funds are held by an in-flight reservation, surface the
  // breakdown so a low "available" (and the insufficient-funds error that quotes
  // it) is explained instead of contradicting the headline balance.
  const reservedNum =
    userReserved != null
      ? Number.parseFloat(userReserved.replace(/[^\d.-]/g, ""))
      : NaN;
  const hasHold = Number.isFinite(reservedNum) && reservedNum > 0;

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const initial = (userName ?? userEmail ?? "?").trim().charAt(0).toUpperCase() || "?";

  return (
    <div ref={containerRef} className="relative border-t border-neutral-700">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        className="flex w-full items-center gap-3 px-3 py-2 text-left transition-colors hover:bg-neutral-700/60"
      >
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-emerald-600/20 text-sm font-semibold text-emerald-400">
          {initial}
        </span>
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="truncate text-sm text-neutral-200">
            {userName ?? "Профиль"}
          </span>
          {userBalance != null ? (
            <span className={`truncate text-xs ${balanceTone}`}>
              {isZeroBalance ? "Баланс исчерпан · " : "Баланс: "}
              <AnimatedBalance apiString={userBalance} />
            </span>
          ) : null}
        </span>
        <svg
          className={`h-4 w-4 shrink-0 text-neutral-500 transition-transform ${open ? "rotate-180" : ""}`}
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
          aria-hidden="true"
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 12l4-4 4 4" />
        </svg>
      </button>

      {open ? (
        <>
          <button
            type="button"
            aria-hidden="true"
            tabIndex={-1}
            onClick={() => setOpen(false)}
            className="fixed inset-0 z-30 cursor-default"
          />
          <div
            role="menu"
            className="absolute bottom-full left-2 right-2 z-40 mb-2 animate-scaleIn overflow-hidden rounded-lg border border-neutral-700 bg-[var(--trinity-popup)] py-1 shadow-2xl"
          >
            <div className="px-3 py-2">
              <p className="truncate text-[11px] font-mono uppercase tracking-wider text-neutral-500">
                Аккаунт
              </p>
              <p className="truncate text-sm text-neutral-400/80" title={userEmail ?? undefined}>
                {userEmail ?? "—"}
              </p>
              {userBalance != null ? (
                <p className={`mt-1.5 text-sm font-medium ${balanceTone}`}>
                  Баланс: {formatBalance(userBalance)}
                </p>
              ) : null}
              {hasHold && userReserved != null && userAvailable != null ? (
                <p className="mt-0.5 text-xs text-amber-400/90">
                  Зарезервировано {formatBalance(userReserved)} · доступно{" "}
                  {formatBalance(userAvailable)}
                </p>
              ) : null}
              {isZeroBalance || isLowBalance ? (
                <p
                  className={`mt-0.5 text-xs ${
                    isZeroBalance ? "text-red-400/90" : "text-amber-400/90"
                  }`}
                >
                  {isZeroBalance
                    ? "Средства закончились — пополните, чтобы отправлять сообщения."
                    : "Баланс на исходе — скоро потребуется пополнение."}
                </p>
              ) : null}
            </div>
            <div className="my-1 border-t border-neutral-700/70" />
            <Link
              href="/settings"
              role="menuitem"
              onClick={() => setOpen(false)}
              className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm text-neutral-200 transition-colors hover:bg-neutral-700"
            >
              <span className="text-neutral-400">
                <SettingsGearIcon />
              </span>
              Настройки
            </Link>
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                onLogout();
              }}
              className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm text-red-400 transition-colors hover:bg-red-900/30"
            >
              <LogoutIcon />
              Выйти
            </button>
          </div>
        </>
      ) : null}
    </div>
  );
}

function Section({
  label,
  items,
  children,
  headerAction,
}: {
  label: string;
  items: ConversationSummary[];
  children: React.ReactNode;
  headerAction?: React.ReactNode;
}) {
  if (items.length === 0) return null;
  return (
    <div>
      <div className="flex items-center justify-between px-3 pb-1 pt-4">
        <span className="text-[11px] font-medium uppercase tracking-wider text-neutral-600">
          {label}
        </span>
        {headerAction}
      </div>
      <ul className="flex flex-col gap-0.5">{children}</ul>
    </div>
  );
}

function ArchiveSection({
  archived,
  isLoading,
  onLoad,
  onRestore,
  onPermanentDelete,
}: {
  archived: ConversationSummary[];
  isLoading: boolean;
  onLoad: () => void;
  onRestore: (id: string) => void;
  onPermanentDelete: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [loadedOnce, setLoadedOnce] = useState(false);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && !loadedOnce) {
      onLoad();
      setLoadedOnce(true);
    }
  };

  return (
    <div className="border-t border-neutral-700">
      <button
        type="button"
        onClick={toggle}
        className="flex w-full items-center justify-between px-4 py-2.5 text-xs font-medium text-neutral-500 transition-colors hover:bg-neutral-700/60 hover:text-neutral-300"
      >
        <span>Архив</span>
        <span aria-hidden="true">{open ? "▾" : "▸"}</span>
      </button>
      {open ? (
        <div className="max-h-48 overflow-y-auto px-2 pb-2">
          {isLoading ? (
            <p className="px-2 py-2 text-xs text-neutral-500">Загрузка…</p>
          ) : archived.length === 0 ? (
            <p className="px-2 py-2 text-xs text-neutral-500">Архив пуст.</p>
          ) : (
            <ul className="flex flex-col gap-0.5">
              {archived.map((c) => (
                <li
                  key={c.id}
                  className="flex items-center justify-between gap-1 rounded-lg px-2 py-2 transition-colors hover:bg-neutral-700/60"
                >
                  <span className="min-w-0 flex-1 truncate text-xs text-neutral-300">
                    {c.title ?? "Новый диалог"}
                  </span>
                  <button
                    type="button"
                    title="Восстановить"
                    aria-label="Восстановить"
                    onClick={() => onRestore(c.id)}
                    className="inline-flex h-6 w-6 items-center justify-center rounded text-neutral-400 transition-colors hover:bg-neutral-700 hover:text-emerald-400 active:scale-[0.97]"
                  >
                    <RestoreIcon />
                  </button>
                  <button
                    type="button"
                    title="Удалить навсегда"
                    aria-label="Удалить навсегда"
                    onClick={() => setConfirmDeleteId(c.id)}
                    className="inline-flex h-6 w-6 items-center justify-center rounded text-neutral-400 transition-colors hover:bg-red-900/40 hover:text-red-400 active:scale-[0.97]"
                  >
                    <TrashIcon />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
      <ConfirmDialog
        open={confirmDeleteId !== null}
        title="Удалить диалог навсегда?"
        message="Это действие необратимо."
        confirmLabel="Удалить навсегда"
        onConfirm={() => {
          if (confirmDeleteId) onPermanentDelete(confirmDeleteId);
          setConfirmDeleteId(null);
        }}
        onCancel={() => setConfirmDeleteId(null)}
      />
    </div>
  );
}

function SidebarRow({
  conversation,
  isActive,
  isPinned,
  onTogglePin,
  onDelete,
  onRename,
}: {
  conversation: ConversationSummary;
  isActive: boolean;
  isPinned: boolean;
  onTogglePin: () => void;
  onDelete: () => void;
  onRename: (title: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);
  const [draft, setDraft] = useState(conversation.title ?? "");
  // a11y (tr-34yj): the "Действия" trigger, so dismissing the menu (Escape /
  // click-away) returns focus to it instead of dropping to <body> (WCAG 2.4.3).
  const menuTriggerRef = useRef<HTMLButtonElement>(null);

  const title = conversation.title ?? "Новый диалог";

  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setMenuOpen(false);
        menuTriggerRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  if (editing) {
    return (
      <li>
        <input
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => {
            if (draft.trim().length > 0 && draft.trim() !== title) {
              onRename(draft.trim());
            }
            setEditing(false);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.currentTarget.blur();
            } else if (e.key === "Escape") {
              setDraft(conversation.title ?? "");
              setEditing(false);
            }
          }}
          className="block w-full rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-neutral-100 outline-none focus:border-emerald-600"
        />
      </li>
    );
  }

  return (
    <li
      className={`group relative rounded-lg transition-colors ${
        isActive ? "bg-neutral-700" : "hover:bg-neutral-700/50"
      }`}
    >
      <Link href={`/chat/${conversation.id}`} className="block px-3 py-2 pr-8">
        <div
          className={`truncate text-sm ${
            isActive ? "font-medium text-neutral-100" : "text-neutral-300"
          }`}
        >
          {title}
        </div>
        <div className="truncate text-[11px] text-neutral-600">
          {formatModelName(conversation.model_id)}
        </div>
      </Link>

      <button
        ref={menuTriggerRef}
        type="button"
        title="Действия"
        aria-label="Действия с диалогом"
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        onClick={(e) => {
          e.preventDefault();
          setMenuOpen((v) => !v);
        }}
        className={`absolute right-1.5 top-2 inline-flex h-6 w-6 items-center justify-center rounded text-neutral-400 transition-all hover:bg-neutral-700 hover:text-neutral-100 active:scale-[0.97] ${
          menuOpen ? "opacity-100" : "opacity-0 group-hover:opacity-100"
        }`}
      >
        <DotsIcon />
      </button>

      {menuOpen ? (
        <>
          {/* Click-away backdrop. */}
          <button
            type="button"
            aria-hidden="true"
            tabIndex={-1}
            onClick={(e) => {
              e.preventDefault();
              setMenuOpen(false);
              menuTriggerRef.current?.focus();
            }}
            className="fixed inset-0 z-30 cursor-default"
          />
          <div
            role="menu"
            className="absolute right-1 top-9 z-40 w-44 overflow-hidden rounded-lg border border-neutral-700 bg-neutral-800 py-1 shadow-xl"
          >
            <button
              type="button"
              role="menuitem"
              onClick={(e) => {
                e.preventDefault();
                setMenuOpen(false);
                onTogglePin();
              }}
              className="flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-sm text-neutral-200 transition-colors hover:bg-neutral-700"
            >
              <PinIcon />
              {isPinned ? "Открепить" : "Закрепить"}
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={(e) => {
                e.preventDefault();
                setMenuOpen(false);
                setDraft(conversation.title ?? "");
                setEditing(true);
              }}
              className="flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-sm text-neutral-200 transition-colors hover:bg-neutral-700"
            >
              <PencilIcon />
              Переименовать
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={(e) => {
                e.preventDefault();
                setMenuOpen(false);
                // tr-zrlp/tr-v42t: a styled confirm modal (not native confirm)
                // before the recoverable soft delete.
                setConfirmDeleteOpen(true);
              }}
              className="flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-sm text-red-400 transition-colors hover:bg-red-900/30"
            >
              <TrashIcon />
              Удалить
            </button>
          </div>
        </>
      ) : null}
      <ConfirmDialog
        open={confirmDeleteOpen}
        title="Удалить диалог?"
        message="Его можно восстановить из архива в течение 30 дней."
        confirmLabel="Удалить"
        onConfirm={() => {
          setConfirmDeleteOpen(false);
          onDelete();
        }}
        onCancel={() => setConfirmDeleteOpen(false)}
      />
    </li>
  );
}

export default ChatSidebar;
