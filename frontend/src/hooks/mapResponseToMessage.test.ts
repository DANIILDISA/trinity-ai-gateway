import { describe, it, expect } from "vitest";

import { applyMessageDone, mapResponseToMessage } from "./useChat";
import type { MessageResponse, SSEMessageDone } from "@/types/api";

const baseResponse: MessageResponse = {
  id: "m-1",
  role: "assistant",
  content: "hello",
  model_id: "claude-opus-4",
  input_tokens: 10,
  output_tokens: 20,
  reasoning_tokens: 5,
  cost_micro_rub: 123456,
  request_id: "req-1",
  thinking: "pondering",
  // version-history fields (migration 014)
  parent_id: "u-1",
  version_index: 1,
  version_count: 3,
  is_active: true,
  web_search: true,
  created_at: "2026-06-08T10:00:00Z",
};

describe("mapResponseToMessage — version-history fields", () => {
  it("maps parent_id/is_active/version_index/version_count onto the ChatMessage", () => {
    const msg = mapResponseToMessage(baseResponse);
    expect(msg.parentId).toBe("u-1");
    expect(msg.isActive).toBe(true);
    expect(msg.versionIndex).toBe(1);
    expect(msg.versionCount).toBe(3);
  });

  it("maps model_id and created_at (camelCase) onto the ChatMessage", () => {
    const msg = mapResponseToMessage(baseResponse);
    expect(msg.modelId).toBe("claude-opus-4");
    expect(msg.createdAt).toBe("2026-06-08T10:00:00Z");
  });

  it("carries through the existing fields unchanged", () => {
    const msg = mapResponseToMessage(baseResponse);
    expect(msg.id).toBe("m-1");
    expect(msg.role).toBe("assistant");
    expect(msg.content).toBe("hello");
    expect(msg.reasoningTokens).toBe(5);
    expect(msg.tokensUsed).toEqual({ input: 10, output: 20 });
  });

  it("maps request_id so the copy-request-id control survives a reload", () => {
    const msg = mapResponseToMessage(baseResponse);
    expect(msg.requestId).toBe("req-1");
  });

  it("maps web_search -> webSearchEnabled so the meta-row globe survives a reload", () => {
    const msg = mapResponseToMessage(baseResponse);
    expect(msg.webSearchEnabled).toBe(true);
    // false on a non-search turn (no globe after reload).
    const noSearch = mapResponseToMessage({ ...baseResponse, web_search: false });
    expect(noSearch.webSearchEnabled).toBe(false);
  });

  it("maps an absent parent_id to undefined (never null)", () => {
    const rooted = { ...baseResponse, parent_id: null };
    const msg = mapResponseToMessage(rooted);
    expect(msg.parentId).toBeUndefined();
  });

  it("leaves version fields undefined when absent on a legacy response", () => {
    const legacy = {
      ...baseResponse,
      parent_id: undefined,
      version_index: undefined,
      version_count: undefined,
      is_active: undefined,
    } as unknown as MessageResponse;
    const msg = mapResponseToMessage(legacy);
    expect(msg.parentId).toBeUndefined();
    expect(msg.versionCount).toBeUndefined();
  });
});

describe("mapResponseToMessage — thinking segments (tr-cy0.3.8)", () => {
  it("maps thinking_segments onto thinkingSegments, null summary → undefined", () => {
    const msg = mapResponseToMessage({
      ...baseResponse,
      thinking_segments: [
        { id: "seg-0", text: "Анализ", summary: "Анализирую вопросы" },
        { id: "seg-1", text: "Ответ", summary: null },
      ],
    });
    expect(msg.thinkingSegments).toEqual([
      { id: "seg-0", text: "Анализ", summary: "Анализирую вопросы" },
      { id: "seg-1", text: "Ответ", summary: undefined },
    ]);
  });

  it("leaves thinkingSegments undefined for a legacy/single-segment row", () => {
    const msg = mapResponseToMessage({ ...baseResponse, thinking_segments: null });
    expect(msg.thinkingSegments).toBeUndefined();
    // The flat thinking text is still carried for the single-segment fallback.
    expect(msg.thinking).toBe("pondering");
  });
});

describe("applyMessageDone — thinking segments (tr-cy0.3.8)", () => {
  const baseDone: SSEMessageDone = {
    request_id: "r1",
    actual_cost: "1 ₽",
    balance_after: "9 ₽",
    tokens_used: { input: 1, output: 1, reasoning: 1 },
    message_id: "real-id",
  };

  it("restores thinkingSegments from the result DTO (post-Stop reconcile)", () => {
    const prev = [
      { id: "a1", role: "assistant" as const, content: "часть" },
    ];
    const [msg] = applyMessageDone(prev, "a1", {
      ...baseDone,
      thinking_segments: [{ id: "seg-0", text: "t", summary: "Заголовок" }],
    });
    expect(msg.id).toBe("real-id");
    expect(msg.thinkingSegments).toEqual([
      { id: "seg-0", text: "t", summary: "Заголовок" },
    ]);
  });

  it("preserves live thinkingSegments when the DTO omits them (live done)", () => {
    const prev = [
      {
        id: "a1",
        role: "assistant" as const,
        content: "x",
        thinkingSegments: [{ id: "seg-0", text: "live", summaryPending: false }],
      },
    ];
    const [msg] = applyMessageDone(prev, "a1", baseDone);
    expect(msg.thinkingSegments).toEqual([
      { id: "seg-0", text: "live", summaryPending: false },
    ]);
  });
});

describe("tool steps mapping (tr-cy0.2.9)", () => {
  it("maps tool_steps onto thinkingSteps on reload", () => {
    const msg = mapResponseToMessage({
      ...baseResponse,
      tool_steps: [
        { id: "tc-0-0", tool: "create_file", title: "Создание файла a.md", filename: "a.md", diff: { added: 2, removed: 0 }, presented: true, status: "done", output: "ok" },
      ],
    });
    expect(msg.thinkingSteps).toEqual([
      { id: "tc-0-0", tool: "create_file", title: "Создание файла a.md", status: "done", filename: "a.md", diff: { added: 2, removed: 0 }, presented: true, output: "ok" },
    ]);
  });

  it("carries run_python input (code), duration and truncated onto thinkingSteps on reload (tr-jk4j/tr-pock)", () => {
    const msg = mapResponseToMessage({
      ...baseResponse,
      tool_steps: [
        {
          id: "tc-0-0",
          tool: "run_python",
          title: "Выполнение Python",
          status: "done",
          output: "4",
          duration_ms: 139,
          truncated: false,
          input: '{"code":"print(2 + 2)"}',
        },
      ],
    });
    expect(msg.thinkingSteps?.[0]).toMatchObject({
      input: '{"code":"print(2 + 2)"}',
      duration_ms: 139,
    });
  });

  it("leaves thinkingSteps undefined when tool_steps absent/null", () => {
    expect(mapResponseToMessage({ ...baseResponse, tool_steps: null }).thinkingSteps).toBeUndefined();
  });

  it("applyMessageDone restores tool_steps from the result DTO (post-Stop)", () => {
    const prev = [{ id: "a1", role: "assistant" as const, content: "x" }];
    const [m] = applyMessageDone(prev, "a1", {
      request_id: "r", actual_cost: "1 ₽", balance_after: "9 ₽",
      tokens_used: { input: 1, output: 1, reasoning: 1 }, message_id: "real",
      tool_steps: [{ id: "tc-0-0", tool: "create_file", title: "t", status: "done" }],
    });
    expect(m.thinkingSteps).toEqual([
      { id: "tc-0-0", tool: "create_file", title: "t", status: "done", filename: undefined, diff: undefined, presented: undefined, output: undefined },
    ]);
  });
});
