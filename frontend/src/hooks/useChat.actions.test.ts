import { describe, expect, it } from "vitest";
import {
  activePathIds,
  applyMessageDone,
  deriveActivePath,
  mapResponseToMessage,
} from "@/hooks/useChat";
import type { ChatMessage } from "@/hooks/useChat";
import type { MessageResponse, SSEMessageDone } from "@/types/api";

function base(id: string, role: "user" | "assistant"): ChatMessage {
  return { id, role, content: id };
}

const DONE: SSEMessageDone = {
  request_id: "r1",
  actual_cost: "0.50 ₽",
  balance_after: "99 ₽",
  tokens_used: { input: 10, output: 5, reasoning: 2 },
  message_id: "db-asst",
  active_leaf_id: "db-asst",
};

describe("applyMessageDone", () => {
  it("swaps the placeholder id for data.message_id and stamps finalize fields", () => {
    const prev: ChatMessage[] = [
      base("user-1", "user"),
      { id: "gen-asst", role: "assistant", content: "ответ", isStreaming: true },
    ];
    const next = applyMessageDone(prev, "gen-asst", DONE);
    const asst = next[1];
    expect(asst.id).toBe("db-asst");
    expect(asst.content).toBe("ответ");
    expect(asst.isStreaming).toBe(false);
    expect(asst.actualCost).toBe("0.50 ₽");
    expect(asst.tokensUsed).toEqual({ input: 10, output: 5 });
    expect(asst.reasoningTokens).toBe(2);
  });

  it("touches ONLY the closured assistant id (switch race guard)", () => {
    const prev: ChatMessage[] = [
      { id: "other-asst", role: "assistant", content: "X", isStreaming: true },
      { id: "gen-asst", role: "assistant", content: "Y", isStreaming: true },
    ];
    const next = applyMessageDone(prev, "gen-asst", DONE);
    expect(next[0]).toBe(prev[0]); // untouched reference
    expect(next[1].id).toBe("db-asst");
  });

  it("falls back to the placeholder id when message_id is absent", () => {
    const prev: ChatMessage[] = [
      { id: "gen-asst", role: "assistant", content: "Z", isStreaming: true },
    ];
    const { message_id, ...legacy } = DONE;
    void message_id;
    const next = applyMessageDone(prev, "gen-asst", legacy as SSEMessageDone);
    expect(next[0].id).toBe("gen-asst");
  });

  it("swaps the user placeholder for data.user_message_id when provided", () => {
    // The send path keeps the user bubble's genId placeholder (it drops the
    // post-stream re-fetch). Without this swap, editing/resending the just-sent
    // message targets a non-existent id → 404.
    const prev: ChatMessage[] = [
      { id: "gen-user", role: "user", content: "вопрос" },
      { id: "gen-asst", role: "assistant", content: "ответ", isStreaming: true },
    ];
    const done: SSEMessageDone = { ...DONE, user_message_id: "db-user" };
    const next = applyMessageDone(prev, "gen-asst", done, "gen-user");
    expect(next[0].id).toBe("db-user"); // user placeholder reconciled
    expect(next[0].role).toBe("user");
    expect(next[1].id).toBe("db-asst"); // assistant still swapped
  });

  it("leaves the user bubble untouched when no userPlaceholderId is passed", () => {
    const prev: ChatMessage[] = [
      { id: "gen-user", role: "user", content: "вопрос" },
      { id: "gen-asst", role: "assistant", content: "ответ", isStreaming: true },
    ];
    const done: SSEMessageDone = { ...DONE, user_message_id: "db-user" };
    const next = applyMessageDone(prev, "gen-asst", done);
    expect(next[0].id).toBe("gen-user"); // unchanged — runActionStream path
  });

  it("keeps the previous reasoningTokens when the frame omits reasoning", () => {
    const prev: ChatMessage[] = [
      {
        id: "gen-asst",
        role: "assistant",
        content: "R",
        isStreaming: true,
        reasoningTokens: 7,
      },
    ];
    const done: SSEMessageDone = {
      ...DONE,
      tokens_used: { input: 1, output: 1 },
    };
    const next = applyMessageDone(prev, "gen-asst", done);
    expect(next[0].reasoningTokens).toBe(7);
  });
});

describe("mapResponseToMessage carries version fields", () => {
  it("maps parent_id/version/is_active onto ChatMessage", () => {
    const r: MessageResponse = {
      id: "m1",
      role: "assistant",
      content: "hi",
      model_id: null,
      cost_micro_rub: null,
      parent_id: "u1",
      version_index: 1,
      version_count: 3,
      is_active: true,
      created_at: "2026-06-08T00:00:00Z",
    } as MessageResponse;
    const m = mapResponseToMessage(r);
    expect(m.parentId).toBe("u1");
    expect(m.versionIndex).toBe(1);
    expect(m.versionCount).toBe(3);
    expect(m.isActive).toBe(true);
  });
});

describe("activePathIds", () => {
  it("returns ids of active messages only", () => {
    const msgs: ChatMessage[] = [
      { id: "a", role: "user", content: "", isActive: true },
      { id: "b", role: "assistant", content: "", isActive: false },
      { id: "c", role: "assistant", content: "" }, // undefined => treated active
    ];
    expect(activePathIds(msgs).sort()).toEqual(["a", "c"]);
  });
});

describe("deriveActivePath", () => {
  // Sibling cache for one turn: two assistant versions answering user "u1".
  const siblings: MessageResponse[] = [
    {
      id: "asst-v1",
      role: "assistant",
      content: "первый ответ",
      model_id: "m",
      cost_micro_rub: null,
      parent_id: "u1",
      version_index: 1,
      version_count: 2,
      is_active: true,
      created_at: "2026-06-08T00:00:00Z",
    } as MessageResponse,
    {
      id: "asst-v2",
      role: "assistant",
      content: "второй ответ",
      model_id: "m",
      cost_micro_rub: null,
      parent_id: "u1",
      version_index: 2,
      version_count: 2,
      is_active: false,
      created_at: "2026-06-08T00:01:00Z",
    } as MessageResponse,
  ];

  it("swaps the active sibling in place when switching to another version", () => {
    const rendered: ChatMessage[] = [
      { id: "u1", role: "user", content: "вопрос", isActive: true },
      mapResponseToMessage(siblings[0]),
    ];
    const next = deriveActivePath(rendered, siblings, "asst-v2");
    expect(next.map((m) => m.id)).toEqual(["u1", "asst-v2"]);
    expect(next[1].content).toBe("второй ответ");
    expect(next[1].isActive).toBe(true);
  });

  it("returns the rendered list unchanged when the node is not a known sibling", () => {
    const rendered: ChatMessage[] = [
      { id: "u1", role: "user", content: "вопрос", isActive: true },
      mapResponseToMessage(siblings[0]),
    ];
    const next = deriveActivePath(rendered, siblings, "unknown-id");
    expect(next).toBe(rendered);
  });
});
