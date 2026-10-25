import { describe, it, expect } from "vitest";

import type { ConversationSummary } from "@/hooks/useConversations";
import { applyChatView, selectRecents, RECENTS_LIMIT } from "./chatList";

// Newest first: index 0 = most recent. updated_at descends with index.
function makeConvs(n: number): ConversationSummary[] {
  return Array.from({ length: n }, (_, i) => ({
    id: `id-${i}`,
    title: `Чат ${String(i).padStart(2, "0")}`,
    model_id: "openai/gpt-4.1-mini",
    updated_at: new Date(Date.UTC(2026, 0, 1, 0, 0, n - i)).toISOString(),
  }));
}

describe("selectRecents", () => {
  it("returns at most RECENTS_LIMIT, newest first", () => {
    const out = selectRecents(makeConvs(25), new Set());
    expect(out).toHaveLength(RECENTS_LIMIT);
    expect(out[0].id).toBe("id-0");
    expect(out[19].id).toBe("id-19");
  });

  it("excludes pinned ids and does not count them toward the limit", () => {
    const convs = makeConvs(22);
    const pinned = new Set(["id-0", "id-1"]);
    const out = selectRecents(convs, pinned);
    expect(out.some((c) => pinned.has(c.id))).toBe(false);
    expect(out).toHaveLength(RECENTS_LIMIT); // 20 of the remaining 20 non-pinned
    expect(out[0].id).toBe("id-2");
  });

  it("returns all when fewer than the limit", () => {
    expect(selectRecents(makeConvs(5), new Set())).toHaveLength(5);
  });

  it("does not mutate the input array", () => {
    const convs = makeConvs(3);
    const copy = [...convs];
    selectRecents(convs, new Set());
    expect(convs).toEqual(copy);
  });
});

describe("applyChatView", () => {
  const convs = makeConvs(4); // id-0 newest … id-3 oldest

  it("recent mode sorts newest first", () => {
    const out = applyChatView(convs, new Set(), "recent", "");
    expect(out.map((c) => c.id)).toEqual(["id-0", "id-1", "id-2", "id-3"]);
  });

  it("oldest mode sorts oldest first", () => {
    const out = applyChatView(convs, new Set(), "oldest", "");
    expect(out.map((c) => c.id)).toEqual(["id-3", "id-2", "id-1", "id-0"]);
  });

  it("title mode sorts by displayed title (ru)", () => {
    const mixed: ConversationSummary[] = [
      { ...convs[0], id: "b", title: "Яблоко" },
      { ...convs[1], id: "a", title: "Арбуз" },
      { ...convs[2], id: "n", title: null }, // → "Новый диалог"
    ];
    const out = applyChatView(mixed, new Set(), "title", "");
    expect(out.map((c) => c.id)).toEqual(["a", "n", "b"]);
  });

  it("pinned mode returns only pinned (newest first)", () => {
    const out = applyChatView(convs, new Set(["id-1", "id-3"]), "pinned", "");
    expect(out.map((c) => c.id)).toEqual(["id-1", "id-3"]);
  });

  it("search filters case-insensitively over the displayed title", () => {
    const out = applyChatView(convs, new Set(), "recent", "ЧАТ 00");
    expect(out.map((c) => c.id)).toEqual(["id-0"]);
  });

  it("search matches the null-title fallback", () => {
    const list: ConversationSummary[] = [{ ...convs[0], id: "x", title: null }];
    expect(applyChatView(list, new Set(), "recent", "новый")).toHaveLength(1);
  });

  it("empty query returns the full mode list", () => {
    expect(applyChatView(convs, new Set(), "recent", "   ")).toHaveLength(4);
  });

  it("does not mutate the input array", () => {
    const copy = [...convs];
    applyChatView(convs, new Set(), "oldest", "");
    expect(convs).toEqual(copy);
  });
});
