"use client";

import Link from "next/link";

import { Spinner } from "@/components/ui/Spinner";
import { formatRelativeDate } from "@/lib/formatting";
import type { ConversationSummary } from "@/hooks/useConversations";
import { EmptyState, Panel, SectionShell } from "./primitives";

export interface HistorySectionProps {
  conversations: ConversationSummary[];
  isLoading: boolean;
}

export function HistorySection({
  conversations,
  isLoading,
}: HistorySectionProps) {
  return (
    <SectionShell
      title="История"
      description="Ваши недавние диалоги."
    >
      <Panel
        title="Диалоги"
        action={
          <Link
            href="/history"
            className="font-mono text-[11px] uppercase tracking-wider text-neutral-500 transition-colors hover:text-emerald-400"
          >
            Вся история →
          </Link>
        }
      >
        {isLoading ? (
          <div className="flex justify-center py-4">
            <Spinner />
          </div>
        ) : conversations.length === 0 ? (
          <EmptyState>У вас пока нет диалогов</EmptyState>
        ) : (
          <ul className="divide-y divide-neutral-800/60">
            {conversations.map((c) => (
              <li
                key={c.id}
                className="grid grid-cols-[1fr,auto,5rem] items-center gap-4 py-2.5"
              >
                <Link
                  href={`/chat/${c.id}`}
                  className="truncate text-sm text-neutral-200 transition-colors hover:text-neutral-50"
                >
                  {c.title ?? "Новый диалог"}
                </Link>
                <span className="font-mono text-[11px] uppercase tracking-wider text-neutral-500">
                  {shortModelName(c.model_id)}
                </span>
                <span className="text-right font-mono text-[11px] tabular-nums text-neutral-500">
                  {formatRelativeDate(c.updated_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </SectionShell>
  );
}

function shortModelName(modelId: string): string {
  const last = modelId.split("/").pop() ?? modelId;
  return last.replace(/-/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());
}
