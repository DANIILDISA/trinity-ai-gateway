import type { ConversationSummary } from "@/hooks/useConversations";

export const RECENTS_LIMIT = 20;

export type ChatViewMode = "recent" | "oldest" | "title" | "pinned";

function displayTitle(c: ConversationSummary): string {
  return c.title ?? "Новый диалог";
}

/** Non-pinned conversations, newest first, capped at `limit`. */
export function selectRecents(
  conversations: ConversationSummary[],
  pinnedIds: Set<string>,
  limit: number = RECENTS_LIMIT,
): ConversationSummary[] {
  return [...conversations]
    .filter((c) => !pinnedIds.has(c.id))
    .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))
    .slice(0, limit);
}

/** Filter (by mode + search) then sort. Pure; never mutates the input. */
export function applyChatView(
  conversations: ConversationSummary[],
  pinnedIds: Set<string>,
  mode: ChatViewMode,
  query: string,
): ConversationSummary[] {
  const q = query.trim().toLowerCase();
  let list = conversations;
  if (mode === "pinned") {
    list = list.filter((c) => pinnedIds.has(c.id));
  }
  if (q.length > 0) {
    list = list.filter((c) => displayTitle(c).toLowerCase().includes(q));
  }
  const sorted = [...list];
  sorted.sort((a, b) => {
    if (mode === "title") {
      return displayTitle(a).localeCompare(displayTitle(b), "ru");
    }
    const ta = Date.parse(a.updated_at);
    const tb = Date.parse(b.updated_at);
    return mode === "oldest" ? ta - tb : tb - ta; // recent + pinned: newest first
  });
  return sorted;
}
