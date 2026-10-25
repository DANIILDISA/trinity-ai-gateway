import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { applyMessageDone, useChat } from "@/hooks/useChat";
import type { ChatMessage } from "@/hooks/useChat";
import type { SSEEventPayload, SSEMessageDone } from "@/types/api";

vi.mock("@/lib/auth", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth")>("@/lib/auth");
  return { ...actual, getAccessToken: () => "test-token" };
});

vi.mock("@/lib/sse", async () => {
  const actual = await vi.importActual<typeof import("@/lib/sse")>("@/lib/sse");
  return { ...actual, streamChatMessage: vi.fn(), streamEdit: vi.fn() };
});

const { streamChatMessage, streamEdit } = await import("@/lib/sse");
const sendMock = streamChatMessage as unknown as ReturnType<typeof vi.fn>;
const editMock = streamEdit as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  sendMock.mockReset();
  editMock.mockReset();
});

// A send that dies BEFORE the server persisted anything (e.g. the Redis
// idempotency gate 503s): the backend saved NO user message, so the optimistic
// user bubble's genId will never be reconciled to a DB id.
function prePersistFailure(): ReturnType<typeof vi.fn> {
  return vi.fn(
    // eslint-disable-next-line require-yield
    async function* (): AsyncGenerator<SSEEventPayload> {
      yield {
        event: "error",
        data: {
          code: "redis_unavailable",
          message: "Сервер перегружен, попробуйте позже",
        },
      };
    },
  );
}

function happySend(): ReturnType<typeof vi.fn> {
  return vi.fn(
    // eslint-disable-next-line require-yield
    async function* (): AsyncGenerator<SSEEventPayload> {
      yield {
        event: "message_done",
        data: {
          request_id: "req-2",
          message_id: "asst-db",
          user_message_id: "user-db",
          active_leaf_id: "asst-db",
          actual_cost: "0.10",
          balance_after: "99.90",
          tokens_used: { input: 1, output: 1, reasoning: 0 },
        },
      };
    },
  );
}

describe("resend of a never-persisted user message (tr-w4fy.1)", () => {
  it("falls back to a fresh send instead of /edit (which would 404 forever)", async () => {
    sendMock.mockImplementation(prePersistFailure());
    const { result } = renderHook(() => useChat());

    await act(async () => {
      await result.current.sendMessage("привет", "model-x", "conv-1");
    });
    await waitFor(() => expect(sendMock).toHaveBeenCalledTimes(1));

    const userBubble = result.current.messages.find((m) => m.role === "user");
    expect(userBubble).toBeDefined();
    // The failed pre-persist send must leave the bubble marked local-only.
    expect(userBubble!.localOnly).toBe(true);

    // Retry via the resend control → must NOT hit /edit with a phantom id.
    sendMock.mockImplementation(happySend());
    await act(async () => {
      await result.current.editAndResend(userBubble!.id, "привет", {
        modelId: "model-x",
      });
    });

    expect(editMock).not.toHaveBeenCalled();
    await waitFor(() => expect(sendMock).toHaveBeenCalledTimes(2));
    // Fresh send carries the same content to the same conversation.
    expect(sendMock.mock.calls[1][0]).toBe("conv-1");
    expect(sendMock.mock.calls[1][1]).toBe("привет");

    // No duplicate user bubbles: the stale optimistic one was replaced.
    const userBubbles = result.current.messages.filter(
      (m) => m.role === "user" && m.content === "привет",
    );
    expect(userBubbles).toHaveLength(1);
    // Reconciled to the real DB id by message_done.
    await waitFor(() => expect(userBubbles[0].id).toBe("user-db"));
  });

  it("keeps using /edit for a persisted (reconciled) user message", async () => {
    sendMock.mockImplementation(happySend());
    editMock.mockImplementation(happySend());
    const { result } = renderHook(() => useChat());

    await act(async () => {
      await result.current.sendMessage("привет", "model-x", "conv-1");
    });
    const userBubble = result.current.messages.find((m) => m.role === "user");
    expect(userBubble!.id).toBe("user-db");
    expect(userBubble!.localOnly).toBeUndefined();

    await act(async () => {
      await result.current.editAndResend(userBubble!.id, "привет", {
        modelId: "model-x",
      });
    });
    expect(editMock).toHaveBeenCalledTimes(1);
    expect(sendMock).toHaveBeenCalledTimes(1); // no fresh-send fallback
  });
});

describe("applyMessageDone clears the localOnly marker on reconcile", () => {
  it("drops localOnly when swapping the user placeholder for the DB id", () => {
    const prev: ChatMessage[] = [
      { id: "gen-user", role: "user", content: "вопрос", localOnly: true },
      { id: "gen-asst", role: "assistant", content: "ответ", isStreaming: true },
    ];
    const done: SSEMessageDone = {
      request_id: "r1",
      actual_cost: "0.50 ₽",
      balance_after: "99 ₽",
      tokens_used: { input: 10, output: 5 },
      message_id: "db-asst",
      active_leaf_id: "db-asst",
      user_message_id: "db-user",
    };
    const next = applyMessageDone(prev, "gen-asst", done, "gen-user");
    expect(next[0].id).toBe("db-user");
    expect(next[0].localOnly).toBeUndefined();
  });
});
