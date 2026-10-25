import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { useChat } from "@/hooks/useChat";
import { apiClient } from "@/lib/api";
import type { SSEEventPayload } from "@/types/api";

vi.mock("@/lib/auth", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth")>("@/lib/auth");
  return { ...actual, getAccessToken: () => "test-token" };
});

vi.mock("@/lib/sse", async () => {
  const actual = await vi.importActual<typeof import("@/lib/sse")>("@/lib/sse");
  return { ...actual, streamRegenerate: vi.fn(), streamEdit: vi.fn() };
});

const { streamRegenerate, streamEdit } = await import("@/lib/sse");
const regenMock = streamRegenerate as unknown as ReturnType<typeof vi.fn>;
const editMock = streamEdit as unknown as ReturnType<typeof vi.fn>;

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

function emitDone(mock: ReturnType<typeof vi.fn>) {
  mock.mockImplementation(async function* (): AsyncGenerator<SSEEventPayload> {
    yield DONE;
  });
}

afterEach(() => {
  regenMock.mockReset();
  editMock.mockReset();
  vi.restoreAllMocks();
});

describe("useChat — regenerate/edit thread the currently-selected model (tr-77u)", () => {
  it("regenerate forwards opts.modelId to streamRegenerate", async () => {
    vi.spyOn(apiClient, "get").mockRejectedValue(new Error("no server"));
    emitDone(regenMock);

    const { result } = renderHook(() => useChat());
    act(() => {
      result.current.setActiveConversation("conv-1");
      result.current.setInitialMessages([
        { id: "u1", role: "user", content: "вопрос" },
        { id: "a1", role: "assistant", content: "старый ответ" },
      ]);
    });
    await act(async () => {
      await result.current.regenerate("a1", { modelId: "anthropic/claude-sonnet-4-6" });
    });

    expect(regenMock).toHaveBeenCalledTimes(1);
    const opts = regenMock.mock.calls[0][3];
    expect(opts.modelId).toBe("anthropic/claude-sonnet-4-6");
  });

  it("editAndResend forwards the current modelId to streamEdit", async () => {
    vi.spyOn(apiClient, "get").mockRejectedValue(new Error("no server"));
    emitDone(editMock);

    const { result } = renderHook(() => useChat());
    act(() => {
      result.current.setActiveConversation("conv-1");
      result.current.setInitialMessages([
        { id: "u1", role: "user", content: "вопрос" },
        { id: "a1", role: "assistant", content: "ответ" },
      ]);
    });
    await act(async () => {
      await result.current.editAndResend("u1", "исправленный вопрос", {
        modelId: "anthropic/claude-sonnet-4-6",
      });
    });

    expect(editMock).toHaveBeenCalledTimes(1);
    // streamEdit(conversationId, userMessageId, content, token, options)
    const optsArg = editMock.mock.calls[0][4];
    expect(optsArg.modelId).toBe("anthropic/claude-sonnet-4-6");
  });
});
