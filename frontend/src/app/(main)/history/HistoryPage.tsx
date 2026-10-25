"use client";

import { useState } from "react";
import Link from "next/link";

import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { formatRelativeDate } from "@/lib/formatting";
import type { ConversationSummary } from "@/hooks/useConversations";

export interface HistoryPageProps {
  conversations: ConversationSummary[];
  isLoading: boolean;
  hasMore: boolean;
  onLoadMore: () => void;
  onDelete: (id: string) => void;
  onRename: (id: string, title: string) => void;
}

function shortModelName(modelId: string): string {
  const last = modelId.split("/").pop() ?? modelId;
  return last
    .replace(/-/g, " ")
    .replace(/\b\w/g, (ch) => ch.toUpperCase());
}

export function HistoryPage({
  conversations,
  isLoading,
  hasMore,
  onLoadMore,
  onDelete,
  onRename,
}: HistoryPageProps) {
  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4 px-4 py-8">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-neutral-100">
          История диалогов
        </h1>
      </header>

      {isLoading && conversations.length === 0 ? (
        <div className="flex justify-center py-12">
          <Spinner size="lg" />
        </div>
      ) : conversations.length === 0 ? (
        <p className="rounded-lg border border-neutral-800 bg-neutral-900 px-4 py-6 text-sm text-neutral-400">
          У вас пока нет диалогов.
        </p>
      ) : (
        <ul className="divide-y divide-neutral-800 overflow-hidden rounded-lg border border-neutral-800 bg-neutral-900">
          {conversations.map((c) => (
            <HistoryRow
              key={c.id}
              conversation={c}
              onDelete={() => onDelete(c.id)}
              onRename={(title) => onRename(c.id, title)}
            />
          ))}
        </ul>
      )}

      {hasMore && !isLoading ? (
        <div className="flex justify-center">
          <Button variant="secondary" size="sm" onClick={onLoadMore}>
            Загрузить ещё
          </Button>
        </div>
      ) : null}

      {isLoading && conversations.length > 0 ? (
        <div className="flex justify-center py-2">
          <Spinner />
        </div>
      ) : null}
    </div>
  );
}

function HistoryRow({
  conversation,
  onDelete,
  onRename,
}: {
  conversation: ConversationSummary;
  onDelete: () => void;
  onRename: (title: string) => void;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(conversation.title ?? "");

  const title = conversation.title ?? "Новый диалог";

  return (
    <li className="grid grid-cols-[1fr,auto,auto,auto] items-center gap-3 px-4 py-3 transition-colors hover:bg-neutral-800/50">
      <div className="min-w-0">
        {editing ? (
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
            className="block w-full rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm text-neutral-100 outline-none focus:border-emerald-600"
          />
        ) : (
          <Link
            href={`/chat/${conversation.id}`}
            className="block truncate text-sm font-medium text-neutral-200 hover:text-neutral-100 hover:underline"
          >
            {title}
          </Link>
        )}
      </div>

      <span className="text-xs text-neutral-500">
        {shortModelName(conversation.model_id)}
      </span>
      <span className="text-xs tabular-nums text-neutral-500">
        {formatRelativeDate(conversation.updated_at)}
      </span>

      {confirmingDelete ? (
        <span className="flex items-center gap-1 text-xs">
          <span className="text-neutral-300">Удалить?</span>
          <button
            type="button"
            onClick={onDelete}
            className="rounded bg-red-600 px-2 py-0.5 text-white hover:bg-red-700"
          >
            Да
          </button>
          <button
            type="button"
            onClick={() => setConfirmingDelete(false)}
            className="rounded border border-neutral-700 px-2 py-0.5 text-neutral-300 hover:bg-neutral-700"
          >
            Нет
          </button>
        </span>
      ) : (
        <span className="flex items-center gap-1">
          <button
            type="button"
            title="Переименовать"
            aria-label="Переименовать"
            onClick={() => {
              setDraft(conversation.title ?? "");
              setEditing(true);
            }}
            className="inline-flex h-7 w-7 items-center justify-center rounded text-sm text-neutral-400 hover:bg-neutral-700 hover:text-neutral-100"
          >
            ✎
          </button>
          <button
            type="button"
            title="Удалить"
            aria-label="Удалить"
            onClick={() => setConfirmingDelete(true)}
            className="inline-flex h-7 w-7 items-center justify-center rounded text-sm text-neutral-400 hover:bg-red-900/40 hover:text-red-400"
          >
            🗑
          </button>
        </span>
      )}
    </li>
  );
}

export default HistoryPage;
