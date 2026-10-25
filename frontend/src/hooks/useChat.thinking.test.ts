import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { useChat } from "@/hooks/useChat";
import type { SSEEventPayload } from "@/types/api";

vi.mock("@/lib/auth", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth")>("@/lib/auth");
  return { ...actual, getAccessToken: () => "test-token" };
});

vi.mock("@/lib/sse", async () => {
  const actual = await vi.importActual<typeof import("@/lib/sse")>("@/lib/sse");
  return { ...actual, streamChatMessage: vi.fn() };
});

const { streamChatMessage } = await import("@/lib/sse");
const streamMock = streamChatMessage as unknown as ReturnType<typeof vi.fn>;

function mockStream(events: SSEEventPayload[]) {
  streamMock.mockImplementation(async function* (): AsyncGenerator<SSEEventPayload> {
    for (const e of events) yield e;
  });
}

const DONE: SSEEventPayload = {
  event: "message_done",
  data: {
    request_id: "r1",
    actual_cost: "1 ₽",
    balance_after: "9 ₽",
    tokens_used: { input: 1, output: 1, reasoning: 1 },
    message_id: "a-real",
    active_leaf_id: "a-real",
    user_message_id: "u-real",
  },
};

afterEach(() => streamMock.mockReset());

describe("useChat — per-segment thinking accumulation (tr-cy0.3.8)", () => {
  it("accumulates multiple segments with titles, clearing summaryPending", async () => {
    mockStream([
      // Implicit segment 0 (no thinking_segment, legacy delta path).
      { event: "thinking_delta", data: { delta: "Анализ. " } },
      { event: "thinking_summary", data: { segment_id: "seg-0", summary: "Анализирую" } },
      // Real boundary to segment 1.
      { event: "thinking_segment", data: { segment_id: "seg-1", index: 1 } },
      { event: "thinking_delta", data: { delta: "Ответ." } },
      { event: "thinking_summary", data: { segment_id: "seg-1", summary: "Готовлю" } },
      { event: "content_delta", data: { delta: "Готово." } },
      DONE,
    ]);

    const { result } = renderHook(() => useChat());
    await act(async () => {
      await result.current.sendMessage("вопрос", "model-x", "conv-1");
    });

    await waitFor(() => {
      const a = result.current.messages.find((m) => m.role === "assistant");
      expect(a?.thinkingSegments).toEqual([
        { id: "seg-0", text: "Анализ. ", summary: "Анализирую", summaryPending: false },
        { id: "seg-1", text: "Ответ.", summary: "Готовлю", summaryPending: false },
      ]);
    });
  });

  it("a thinking_delta-only stream yields exactly one segment + flat thinking", async () => {
    mockStream([
      { event: "thinking_delta", data: { delta: "Думаю." } },
      { event: "thinking_delta", data: { delta: " Ещё." } },
      { event: "content_delta", data: { delta: "Готово." } },
      DONE,
    ]);

    const { result } = renderHook(() => useChat());
    await act(async () => {
      await result.current.sendMessage("вопрос", "model-x", "conv-1");
    });

    await waitFor(() => {
      const a = result.current.messages.find((m) => m.role === "assistant");
      expect(a?.thinkingSegments).toEqual([
        { id: "seg-0", text: "Думаю. Ещё.", summaryPending: false },
      ]);
      expect(a?.thinking).toBe("Думаю. Ещё.");
    });
  });

  it("shows summaryPending=true on a freshly opened segment before its summary", async () => {
    // Stream stops right after opening seg-1 (no summary yet): the UI can render a
    // placeholder instead of flashing the fallback.
    mockStream([
      { event: "thinking_delta", data: { delta: "A" } },
      { event: "thinking_segment", data: { segment_id: "seg-1", index: 1 } },
      { event: "thinking_delta", data: { delta: "B" } },
      { event: "content_delta", data: { delta: "ans" } },
      DONE,
    ]);

    const { result } = renderHook(() => useChat());
    await act(async () => {
      await result.current.sendMessage("q", "model-x", "conv-1");
    });

    await waitFor(() => {
      const a = result.current.messages.find((m) => m.role === "assistant");
      expect(a?.thinkingSegments?.[1]).toEqual({
        id: "seg-1",
        text: "B",
        summaryPending: true,
      });
    });
  });
});

describe("useChat — agentic tool-step accumulation (tr-cy0.2.9)", () => {
  it("accumulates a tool step (start → args → result) into thinkingSteps", async () => {
    mockStream([
      { event: "tool_call_start", data: { step_id: "tc-0-0", tool: "create_file", title: "Создание файла a.md" } },
      { event: "tool_call_arguments_delta", data: { step_id: "tc-0-0", delta: '{"path":"a.md"}' } },
      { event: "tool_result", data: { step_id: "tc-0-0", status: "done", filename: "a.md", diff: { added: 2, removed: 0 }, presented: true, output: "ok" } },
      { event: "content_delta", data: { delta: "Готово." } },
      DONE,
    ]);

    const { result } = renderHook(() => useChat());
    await act(async () => {
      await result.current.sendMessage("сделай файл", "model-x", "conv-1");
    });

    await waitFor(() => {
      const a = result.current.messages.find((m) => m.role === "assistant");
      expect(a?.thinkingSteps).toHaveLength(1);
      expect(a?.thinkingSteps?.[0]).toMatchObject({
        id: "tc-0-0",
        tool: "create_file",
        title: "Создание файла a.md",
        status: "done",
        filename: "a.md",
        diff: { added: 2, removed: 0 },
        presented: true,
      });
    });
  });

  it("marks a running step as error when the loop halts", async () => {
    mockStream([
      { event: "tool_call_start", data: { step_id: "tc-0-0", tool: "create_file", title: "x" } },
      { event: "tool_loop_halted", data: { reason: "max_rounds" } },
      { event: "content_delta", data: { delta: "стоп" } },
      DONE,
    ]);
    const { result } = renderHook(() => useChat());
    await act(async () => {
      await result.current.sendMessage("q", "model-x", "conv-1");
    });
    await waitFor(() => {
      const a = result.current.messages.find((m) => m.role === "assistant");
      expect(a?.thinkingSteps?.[0].status).toBe("error");
    });
  });
});
