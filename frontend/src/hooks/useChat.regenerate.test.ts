import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { useChat } from "@/hooks/useChat";
import { apiClient } from "@/lib/api";
import type { ConversationResponse, SSEEventPayload } from "@/types/api";

vi.mock("@/lib/auth", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth")>("@/lib/auth");
  return { ...actual, getAccessToken: () => "test-token" };
});

vi.mock("@/lib/sse", async () => {
  const actual = await vi.importActual<typeof import("@/lib/sse")>("@/lib/sse");
  return { ...actual, streamRegenerate: vi.fn() };
});

const { streamRegenerate } = await import("@/lib/sse");
const regenMock = streamRegenerate as unknown as ReturnType<typeof vi.fn>;

function mockRegen(events: SSEEventPayload[]) {
  regenMock.mockImplementation(async function* (): AsyncGenerator<SSEEventPayload> {
    for (const e of events) yield e;
  });
}

const DONE: SSEEventPayload = {
  event: "message_done",
  data: {
    request_id: "r1",
    actual_cost: "1 ₽",
    balance_after: "9 ₽",
    tokens_used: { input: 1, output: 1, reasoning: 0 },
    message_id: "a-real",
    active_leaf_id: "a-real",
  },
};

afterEach(() => {
  regenMock.mockReset();
  vi.restoreAllMocks();
});

async function seedAndRegenerate(events: SSEEventPayload[]) {
  // Reject every post-stream GET (no server) so the streamed bubble — not a
  // re-fetched active path — is what we assert on.
  vi.spyOn(apiClient, "get").mockRejectedValue(new Error("no server"));
  mockRegen(events);

  const { result } = renderHook(() => useChat());
  act(() => {
    result.current.setActiveConversation("conv-1");
    result.current.setInitialMessages([
      { id: "u1", role: "user", content: "сделай файл" },
      { id: "a1", role: "assistant", content: "старый ответ" },
    ]);
  });
  await act(async () => {
    await result.current.regenerate("a1");
  });
  return result;
}

describe("useChat — runActionStream accumulates tool/thinking on regenerate (tr-1hm)", () => {
  it("builds thinkingSteps from tool events on a regenerated version", async () => {
    const result = await seedAndRegenerate([
      { event: "message_start", data: { request_id: "r1", estimated_cost: "0" } },
      { event: "tool_call_start", data: { step_id: "tc-0-0", tool: "create_file", title: "Создание a.md" } },
      { event: "tool_call_arguments_delta", data: { step_id: "tc-0-0", delta: '{"path":"a.md"}' } },
      { event: "tool_result", data: { step_id: "tc-0-0", status: "done", filename: "a.md", presented: true } },
      { event: "content_delta", data: { delta: "Готово." } },
      DONE,
    ]);

    const a = result.current.messages.find((m) => m.role === "assistant");
    expect(a?.thinkingSteps).toHaveLength(1);
    expect(a?.thinkingSteps?.[0]).toMatchObject({
      id: "tc-0-0",
      tool: "create_file",
      status: "done",
      filename: "a.md",
      presented: true,
    });
    expect(a?.content).toContain("Готово.");
  });

  it("builds thinkingSegments from thinking events on a regenerated version", async () => {
    const result = await seedAndRegenerate([
      { event: "thinking_delta", data: { delta: "Разбираю." } },
      { event: "thinking_summary", data: { segment_id: "seg-0", summary: "Анализ" } },
      { event: "content_delta", data: { delta: "Ответ." } },
      DONE,
    ]);

    const a = result.current.messages.find((m) => m.role === "assistant");
    expect(a?.thinkingSegments).toEqual([
      { id: "seg-0", text: "Разбираю.", summary: "Анализ", summaryPending: false },
    ]);
  });
});

describe("useChat — error/abort re-syncs the restored active path (tr-dfjx)", () => {
  it("shows the server-restored answer after a regenerate error, without a reload", async () => {
    // After a failed regenerate the server restores the prior answer as active
    // (tr-hvr1); the post-stream GET returns that restored conversation.
    const restored: ConversationResponse = {
      id: "conv-1",
      title: null,
      model_id: "m",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      messages: [
        { id: "u1", role: "user", content: "сделай файл", model_id: null, cost_micro_rub: null, created_at: "2026-01-01T00:00:00Z" },
        { id: "a1", role: "assistant", content: "старый ответ", model_id: "m", cost_micro_rub: 1, created_at: "2026-01-01T00:00:00Z" },
      ],
    };
    const getSpy = vi.spyOn(apiClient, "get").mockResolvedValue(restored as never);
    mockRegen([
      { event: "message_start", data: { request_id: "r1", estimated_cost: "0" } },
      { event: "error", data: { code: "insufficient_funds", required: "5 ₽", available: "1 ₽" } },
    ]);

    const { result } = renderHook(() => useChat());
    act(() => {
      result.current.setActiveConversation("conv-1");
      result.current.setInitialMessages([
        { id: "u1", role: "user", content: "сделай файл" },
        { id: "a1", role: "assistant", content: "старый ответ" },
      ]);
    });
    await act(async () => {
      await result.current.regenerate("a1");
    });

    // The post-stream re-sync ran despite the error...
    expect(getSpy).toHaveBeenCalledWith("/api/v1/chat/conversations/conv-1");
    // ...so the restored answer is shown instead of an empty/errored placeholder.
    const assistant = result.current.messages.find((m) => m.role === "assistant");
    expect(assistant?.content).toBe("старый ответ");
    expect(assistant?.errorMessage).toBeUndefined();
  });

  it("polls the active-path GET until it's coherent, not rendering the pre-restore empty slot (tr-i1ve)", async () => {
    // The server's restore commits a beat AFTER the abort, on a fresh shielded
    // session. The FIRST GET reads the pre-restore state (active leaf still on the
    // user message — no assistant for the turn); the SECOND returns the restored
    // assistant. The re-sync must wait for the coherent (assistant-tailed) path
    // instead of rendering the empty slot + transient error until reload.
    const base = {
      id: "conv-1",
      title: null,
      model_id: "m",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    };
    const incoherent: ConversationResponse = {
      ...base,
      messages: [
        { id: "u1", role: "user", content: "сделай файл", model_id: null, cost_micro_rub: null, created_at: "2026-01-01T00:00:00Z" },
      ],
    } as ConversationResponse;
    const coherent: ConversationResponse = {
      ...base,
      messages: [
        { id: "u1", role: "user", content: "сделай файл", model_id: null, cost_micro_rub: null, created_at: "2026-01-01T00:00:00Z" },
        { id: "a1", role: "assistant", content: "старый ответ", model_id: "m", cost_micro_rub: 1, created_at: "2026-01-01T00:00:00Z" },
      ],
    } as ConversationResponse;
    // URL-aware: the image/file loaders (fired just before the re-sync) also call
    // apiClient.get, so a bare mockResolvedValueOnce would be consumed by them.
    // Only the conversation GET drives the poll: incoherent first, coherent after.
    let convGetCount = 0;
    const getSpy = vi
      .spyOn(apiClient, "get")
      .mockImplementation(async (url: string) => {
        if (url === "/api/v1/chat/conversations/conv-1") {
          convGetCount += 1;
          return (convGetCount === 1 ? incoherent : coherent) as never;
        }
        return [] as never; // images/files loaders expect an array
      });
    mockRegen([
      { event: "message_start", data: { request_id: "r1", estimated_cost: "0" } },
      { event: "error", data: { code: "insufficient_funds", required: "5 ₽", available: "1 ₽" } },
    ]);

    const { result } = renderHook(() => useChat());
    act(() => {
      result.current.setActiveConversation("conv-1");
      result.current.setInitialMessages([
        { id: "u1", role: "user", content: "сделай файл" },
        { id: "a1", role: "assistant", content: "старый ответ" },
      ]);
    });
    await act(async () => {
      await result.current.regenerate("a1");
    });

    // Polled past the first incoherent read to the restored answer.
    const convGets = getSpy.mock.calls.filter(
      ([u]) => String(u) === "/api/v1/chat/conversations/conv-1",
    );
    expect(convGets.length).toBeGreaterThanOrEqual(2);
    const assistant = result.current.messages.find((m) => m.role === "assistant");
    expect(assistant?.content).toBe("старый ответ");
  });
});
