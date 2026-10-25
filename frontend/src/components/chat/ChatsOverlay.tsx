"use client";

import { useEffect, useRef, useState } from "react";

import type { ConversationSummary } from "@/hooks/useConversations";
import { applyChatView, type ChatViewMode } from "@/lib/chatList";
import { formatRelativeDate } from "@/lib/formatting";

export interface ChatsOverlayProps {
  conversations: ConversationSummary[];
  pinnedIds: Set<string>;
  isLoading: boolean;
  currentConversationId: string | null;
  onClose(): void;
  onOpenChat(id: string): void;
  onNewChat(): void;
  onTogglePin(id: string): void;
  onRename(id: string, title: string): void;
  onDelete(id: string): void;
  onBulkDelete(ids: string[]): void;
  onLoadAll(): void;
}

const VIEW_LABELS: Record<ChatViewMode, string> = {
  recent: "Недавние",
  oldest: "Сначала старые",
  title: "По названию",
  pinned: "Закреплённые",
};
const VIEW_ORDER: ChatViewMode[] = ["recent", "oldest", "title", "pinned"];

const BTN_SECONDARY =
  "inline-flex items-center gap-1.5 rounded-lg border border-neutral-700 bg-neutral-800 px-3 py-1.5 text-sm text-neutral-200 transition-colors hover:bg-neutral-700 disabled:opacity-50";
const BTN_PRIMARY =
  "inline-flex items-center rounded-lg bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 transition-colors hover:bg-white";
const BTN_DANGER =
  "inline-flex items-center rounded-lg border border-red-900/50 bg-red-900/20 px-3 py-1.5 text-sm text-red-300 transition-colors hover:bg-red-900/40 disabled:opacity-50";
const BTN_GHOST =
  "inline-flex items-center rounded-lg px-3 py-1.5 text-sm text-neutral-400 transition-colors hover:text-neutral-100";

function CloseIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="11" cy="11" r="7" stroke="currentColor" strokeWidth="1.5" />
      <path d="M21 21l-4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M5 12l5 5L20 7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
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

export function ChatsOverlay({
  conversations,
  pinnedIds,
  isLoading,
  currentConversationId,
  onClose,
  onOpenChat,
  onNewChat,
  onTogglePin,
  onRename,
  onDelete,
  onBulkDelete,
  onLoadAll,
}: ChatsOverlayProps) {
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<ChatViewMode>("recent");
  const [filterOpen, setFilterOpen] = useState(false);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const searchRef = useRef<HTMLInputElement | null>(null);

  // Load the full list once on open; focus search.
  useEffect(() => {
    onLoadAll();
    searchRef.current?.focus();
  }, [onLoadAll]);

  // Escape closes the overlay.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const list = applyChatView(conversations, pinnedIds, mode, query);
  const selectedIds = [...selected];
  const allPinnedSelected =
    selectedIds.length > 0 && selectedIds.every((id) => pinnedIds.has(id));

  const toggleSelected = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const exitSelection = () => {
    setSelectionMode(false);
    setSelected(new Set());
  };

  const handleBulkPin = () => {
    // Every selected pinned → unpin all; otherwise pin the not-yet-pinned.
    selectedIds.forEach((id) => {
      const pinned = pinnedIds.has(id);
      if (allPinnedSelected ? pinned : !pinned) onTogglePin(id);
    });
  };

  const handleBulkDelete = () => {
    if (selectedIds.length === 0) return;
    onBulkDelete(selectedIds);
    exitSelection();
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Чаты"
      className="absolute inset-0 z-40 flex flex-col bg-[var(--trinity-canvas)] animate-fadeIn"
    >
      {/* Header */}
      <div className="flex items-center justify-between gap-3 px-6 pb-3 pt-6">
        <h1 className="text-2xl font-semibold tracking-tight text-neutral-100">Чаты</h1>

        {selectionMode ? (
          <div className="flex items-center gap-2">
            <span className="mr-1 text-sm text-neutral-400">Выбрано: {selected.size}</span>
            <button
              type="button"
              onClick={() => setSelected(new Set(list.map((c) => c.id)))}
              className={BTN_SECONDARY}
            >
              Выбрать все
            </button>
            <button
              type="button"
              onClick={handleBulkPin}
              disabled={selected.size === 0}
              className={BTN_SECONDARY}
            >
              {allPinnedSelected ? "Открепить" : "Закрепить"}
            </button>
            <button
              type="button"
              onClick={handleBulkDelete}
              disabled={selected.size === 0}
              className={BTN_DANGER}
            >
              Удалить ({selected.size})
            </button>
            <button type="button" onClick={exitSelection} className={BTN_GHOST}>
              Отмена
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-2">
            <div className="relative">
              <button
                type="button"
                onClick={() => setFilterOpen((v) => !v)}
                aria-haspopup="menu"
                aria-expanded={filterOpen}
                className={BTN_SECONDARY}
              >
                Фильтр: {VIEW_LABELS[mode]}
                <span aria-hidden="true">▾</span>
              </button>
              {filterOpen ? (
                <>
                  <button
                    type="button"
                    aria-hidden="true"
                    tabIndex={-1}
                    onClick={() => setFilterOpen(false)}
                    className="fixed inset-0 z-40 cursor-default"
                  />
                  <div
                    role="menu"
                    className="absolute right-0 top-full z-50 mt-1 w-48 overflow-hidden rounded-lg border border-neutral-700 bg-neutral-800 py-1 shadow-xl"
                  >
                    {VIEW_ORDER.map((m) => (
                      <button
                        key={m}
                        type="button"
                        role="menuitemradio"
                        aria-checked={mode === m}
                        onClick={() => {
                          setMode(m);
                          setFilterOpen(false);
                        }}
                        className={`flex w-full items-center justify-between px-3 py-1.5 text-left text-sm transition-colors hover:bg-neutral-700 ${
                          mode === m ? "text-neutral-100" : "text-neutral-300"
                        }`}
                      >
                        {VIEW_LABELS[m]}
                        {mode === m ? <CheckIcon /> : null}
                      </button>
                    ))}
                  </div>
                </>
              ) : null}
            </div>
            <button type="button" onClick={() => setSelectionMode(true)} className={BTN_SECONDARY}>
              Выбрать
            </button>
            <button type="button" onClick={onNewChat} className={BTN_PRIMARY}>
              Новый чат
            </button>
            <button
              type="button"
              onClick={onClose}
              aria-label="Закрыть"
              className="inline-flex h-9 w-9 items-center justify-center rounded-lg text-neutral-400 transition-colors hover:bg-neutral-800 hover:text-neutral-100"
            >
              <CloseIcon />
            </button>
          </div>
        )}
      </div>

      {/* Search */}
      <div className="px-6 pb-3">
        <div className="relative">
          <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-neutral-500">
            <SearchIcon />
          </span>
          <input
            ref={searchRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Поиск чатов…"
            className="w-full rounded-lg border border-neutral-700 bg-neutral-900 py-2.5 pl-10 pr-3 text-sm text-neutral-100 outline-none placeholder:text-neutral-500 focus:border-neutral-500"
          />
        </div>
      </div>

      {/* List */}
      <div className="min-h-0 flex-1 overflow-y-auto px-6 pb-6">
        {isLoading && conversations.length === 0 ? (
          <p className="py-6 text-sm text-neutral-500">Загрузка…</p>
        ) : list.length === 0 ? (
          <p className="py-6 text-sm text-neutral-500">
            {query.trim() ? "Ничего не найдено" : "Чатов нет"}
          </p>
        ) : (
          <ul className="flex flex-col">
            {list.map((c) => (
              <OverlayRow
                key={c.id}
                conversation={c}
                isActive={c.id === currentConversationId}
                isPinned={pinnedIds.has(c.id)}
                selectionMode={selectionMode}
                isSelected={selected.has(c.id)}
                onToggleSelected={() => toggleSelected(c.id)}
                onOpen={() => onOpenChat(c.id)}
                onTogglePin={() => onTogglePin(c.id)}
                onRename={(t) => onRename(c.id, t)}
                onDelete={() => onDelete(c.id)}
              />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function OverlayRow({
  conversation,
  isActive,
  isPinned,
  selectionMode,
  isSelected,
  onToggleSelected,
  onOpen,
  onTogglePin,
  onRename,
  onDelete,
}: {
  conversation: ConversationSummary;
  isActive: boolean;
  isPinned: boolean;
  selectionMode: boolean;
  isSelected: boolean;
  onToggleSelected: () => void;
  onOpen: () => void;
  onTogglePin: () => void;
  onRename: (title: string) => void;
  onDelete: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [draft, setDraft] = useState(conversation.title ?? "");
  const title = conversation.title ?? "Новый диалог";

  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  if (editing) {
    return (
      <li className="py-1">
        <input
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => {
            if (draft.trim().length > 0 && draft.trim() !== title) onRename(draft.trim());
            setEditing(false);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") e.currentTarget.blur();
            else if (e.key === "Escape") {
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
      className={`group relative flex items-center gap-2 border-b border-neutral-800/60 ${
        isActive ? "bg-neutral-800/40" : "hover:bg-neutral-800/30"
      }`}
    >
      {selectionMode ? (
        <label className="flex cursor-pointer items-center pl-2">
          <input
            type="checkbox"
            checked={isSelected}
            onChange={onToggleSelected}
            aria-label={`Выбрать «${title}»`}
            className="h-4 w-4 accent-emerald-500"
          />
        </label>
      ) : null}

      <button
        type="button"
        onClick={selectionMode ? onToggleSelected : onOpen}
        className="flex min-w-0 flex-1 items-center justify-between gap-4 px-2 py-3 text-left"
      >
        <span className={`min-w-0 truncate text-sm ${isActive ? "text-neutral-100" : "text-neutral-200"}`}>
          {title}
        </span>
        <span className="shrink-0 text-xs text-neutral-500">
          {formatRelativeDate(conversation.updated_at)}
        </span>
      </button>

      {!selectionMode ? (
        <button
          type="button"
          aria-label="Действия с диалогом"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((v) => !v)}
          className={`mr-2 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded text-neutral-400 transition-all hover:bg-neutral-700 hover:text-neutral-100 ${
            menuOpen ? "opacity-100" : "opacity-0 group-hover:opacity-100"
          }`}
        >
          <DotsIcon />
        </button>
      ) : null}

      {menuOpen ? (
        <>
          <button
            type="button"
            aria-hidden="true"
            tabIndex={-1}
            onClick={() => setMenuOpen(false)}
            className="fixed inset-0 z-40 cursor-default"
          />
          <div
            role="menu"
            className="absolute right-2 top-12 z-50 w-44 overflow-hidden rounded-lg border border-neutral-700 bg-neutral-800 py-1 shadow-xl"
          >
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setMenuOpen(false);
                onTogglePin();
              }}
              className="flex w-full items-center px-3 py-1.5 text-left text-sm text-neutral-200 transition-colors hover:bg-neutral-700"
            >
              {isPinned ? "Открепить" : "Закрепить"}
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setMenuOpen(false);
                setDraft(conversation.title ?? "");
                setEditing(true);
              }}
              className="flex w-full items-center px-3 py-1.5 text-left text-sm text-neutral-200 transition-colors hover:bg-neutral-700"
            >
              Переименовать
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setMenuOpen(false);
                onDelete();
              }}
              className="flex w-full items-center px-3 py-1.5 text-left text-sm text-red-400 transition-colors hover:bg-red-900/30"
            >
              Удалить
            </button>
          </div>
        </>
      ) : null}
    </li>
  );
}

export default ChatsOverlay;
