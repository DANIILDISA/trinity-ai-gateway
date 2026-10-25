import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { useChat } from "@/hooks/useChat";
import { ApiRequestError, apiClient } from "@/lib/api";
import type {
  ConversationResponse,
  SSEEventPayload,
  SSEMessageDone,
} from "@/types/api";

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

afterEach(() => {
  streamMock.mockReset();
});

describe("useChat — Stop reconciles balance", () => {
  it("calls onMessageDone after a Stop so the partial-billing balance is refreshed", async () => {
    // Mimic the real stream: yield a content delta, then on abort the SSE layer
    // swallows the AbortError and the generator simply returns (no throw, no
    // error event) — exactly how streamSse behaves (sse.ts:79,122). The dead
    // `aborted` flag (tr-9i5) meant the post-Stop balance refresh never fired.
    streamMock.mockImplementation(
      // eslint-disable-next-line require-yield
      async function* (
        _conversationId: string,
        _content: unknown,
        _model: string,
        _token: string,
        options: { signal: AbortSignal },
      ): AsyncGenerator<SSEEventPayload> {
        yield { event: "content_delta", data: { delta: "часть ответа" } };
        await new Promise<void>((resolve) => {
          if (options.signal.aborted) return resolve();
          options.signal.addEventListener("abort", () => resolve(), {
            once: true,
          });
        });
        // Return WITHOUT yielding message_done — a genuine mid-stream Stop.
      },
    );

    const onMessageDone = vi.fn();
    const { result } = renderHook(() => useChat(onMessageDone));

    let sendPromise: Promise<void> = Promise.resolve();
    act(() => {
      sendPromise = result.current.sendMessage("привет", "model-x", "conv-1");
    });

    await waitFor(() => expect(result.current.isStreaming).toBe(true));

    act(() => {
      result.current.stop();
    });
    await act(async () => {
      await sendPromise;
    });

    // The stream was stopped without a message_done event, so the ONLY path that
    // can fire onMessageDone is the post-Stop reconciliation branch.
    expect(onMessageDone).toHaveBeenCalledTimes(1);
    expect(result.current.isStreaming).toBe(false);
  });
});

describe("useChat — Stop coerces a running tool step (tr-3fh)", () => {
  it("marks a still-running tool step as error after a mid-cycle Stop", async () => {
    // Live (Haiku, tools): Stop mid agentic cycle left one tool step pulsing in
    // `running` forever next to a «Готово» rail. The live bubble must reconcile
    // any still-running step to `error` on abort.
    streamMock.mockImplementation(
      async function* (
        _conversationId: string,
        _content: unknown,
        _model: string,
        _token: string,
        options: { signal: AbortSignal },
      ): AsyncGenerator<SSEEventPayload> {
        yield {
          event: "tool_call_start",
          data: { step_id: "tc-0-0", tool: "str_replace", title: "Правка файла" },
        };
        await new Promise<void>((resolve) => {
          if (options.signal.aborted) return resolve();
          options.signal.addEventListener("abort", () => resolve(), { once: true });
        });
        // Return WITHOUT tool_result or message_done — a genuine mid-cycle Stop.
      },
    );

    const { result } = renderHook(() => useChat());

    let sendPromise: Promise<void> = Promise.resolve();
    act(() => {
      sendPromise = result.current.sendMessage("сделай файл", "model-x", "conv-1");
    });
    await waitFor(() => expect(result.current.isStreaming).toBe(true));

    act(() => {
      result.current.stop();
    });
    await act(async () => {
      await sendPromise;
    });

    const asst = result.current.messages.find((m) => m.role === "assistant");
    const steps = asst?.thinkingSteps ?? [];
    expect(steps).toHaveLength(1);
    expect(steps[0].status).toBe("error");
    expect(steps.some((s) => s.status === "running")).toBe(false);
  });
});

describe("useChat — Stop reconciles bubble badges (tr-sgz)", () => {
  it("patches the assistant bubble with real id/cost/tokens after Stop via polling", async () => {
    // Stream: yield message_start (so requestIdForPoll is set), then one content
    // delta, then block until abort — simulating a genuine mid-stream Stop.
    streamMock.mockImplementation(
      async function* (
        _conversationId: string,
        _content: unknown,
        _model: string,
        _token: string,
        options: { signal: AbortSignal },
      ): AsyncGenerator<SSEEventPayload> {
        yield { event: "message_start", data: { request_id: "req-1", estimated_cost: "0" } };
        yield { event: "content_delta", data: { delta: "частичный ответ" } };
        await new Promise<void>((resolve) => {
          if (options.signal.aborted) return resolve();
          options.signal.addEventListener("abort", () => resolve(), { once: true });
        });
        // Return WITHOUT message_done — the server persisted the partial.
      },
    );

    // Mock the poll endpoint to resolve immediately with the finalized data.
    // reconcileAfterStop does: await setTimeout(500) then apiClient.get(...)
    // We use fake timers with shouldAdvanceTime so waitFor keeps polling while
    // we can still skip the 500ms delay by advancing timers manually (tr-sgz).
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const getSpy = vi.spyOn(apiClient, "get").mockResolvedValue({
      request_id: "req-1",
      message_id: "real-asst-id",
      user_message_id: "user-1",
      active_leaf_id: "real-asst-id",
      actual_cost: "3.5974",
      balance_after: "100.00",
      tokens_used: { input: 3707, output: 2038, reasoning: 0 },
    } satisfies SSEMessageDone);

    try {
      const { result } = renderHook(() => useChat());

      let sendPromise: Promise<void> = Promise.resolve();
      // Start streaming inside act so React batches the initial state update.
      act(() => {
        sendPromise = result.current.sendMessage("привет", "model-x", "conv-1");
      });

      // Wait for the streaming state to go live.
      await waitFor(() => expect(result.current.isStreaming).toBe(true));

      // Abort the stream.
      act(() => {
        result.current.stop();
      });

      // Let the send promise settle (stream loop exits after abort).
      await act(async () => {
        await sendPromise;
      });

      // Advance past the reconcileAfterStop 500ms poll delay.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(600);
      });

      // The poll resolved immediately (mockResolvedValue), so after the timer
      // advance the setMessages call should have landed.
      expect(getSpy).toHaveBeenCalledWith(
        "/api/v1/chat/conversations/conv-1/requests/req-1",
      );

      // The assistant bubble should now carry the real DB id and cost/token badges.
      const asst = result.current.messages.find((m) => m.role === "assistant");
      expect(asst?.id).toBe("real-asst-id");
      expect(asst?.actualCost).toBe("3.5974");
      expect(asst?.tokensUsed).toEqual({ input: 3707, output: 2038 });
    } finally {
      vi.useRealTimers();
      getSpy.mockRestore();
    }
  });
});

// A stream-with-message_start that blocks until abort, then returns without a
// message_done — the shared "genuine mid-stream Stop" shape used below.
function blockingStreamWithStart(): ReturnType<typeof vi.fn> {
  return vi.fn(async function* (
    _conversationId: string,
    _content: unknown,
    _model: string,
    _token: string,
    options: { signal: AbortSignal },
  ): AsyncGenerator<SSEEventPayload> {
    yield { event: "message_start", data: { request_id: "req-1", estimated_cost: "0" } };
    yield { event: "content_delta", data: { delta: "частичный ответ" } };
    await new Promise<void>((resolve) => {
      if (options.signal.aborted) return resolve();
      options.signal.addEventListener("abort", () => resolve(), { once: true });
    });
  });
}

describe("useChat — Stop reconcile gives up cleanly on 404 (tr-kl5)", () => {
  it("polls the result endpoint ONCE on 404, then reconciles via a conversation refetch (tr-kl5 / tr-hsg.13)", async () => {
    // Orphaned/early-stopped request: no persisted result was ever saved, so the
    // request-result endpoint 404s forever. A 404 is terminal — the poll must NOT
    // retry 5× (tr-kl5). But the user message WAS saved before streaming, so the
    // reconcile then refetches the conversation and replaces the optimistic genIds
    // with the real transcript — otherwise a follow-up edit/regenerate 404s and
    // forces a reload (tr-hsg.13).
    streamMock.mockImplementation(blockingStreamWithStart());

    vi.useFakeTimers({ shouldAdvanceTime: true });
    const getSpy = vi
      .spyOn(apiClient, "get")
      .mockImplementation(async (url: string) => {
        if (url.includes("/requests/")) {
          throw new ApiRequestError("Запрос не найден", 404, "not_found");
        }
        // Conversation reconcile refetch: server holds only the persisted user
        // message (the early Stop saved no assistant row).
        return {
          id: "conv-1",
          title: null,
          model_id: "model-x",
          created_at: "2026-06-17T00:00:00Z",
          updated_at: "2026-06-17T00:00:00Z",
          messages: [
            {
              id: "real-user-id",
              role: "user",
              content: "привет",
              model_id: null,
              input_tokens: null,
              output_tokens: null,
              cost_micro_rub: null,
              request_id: null,
              created_at: "2026-06-17T00:00:00Z",
            },
          ],
        } as unknown as ConversationResponse;
      });

    try {
      const { result } = renderHook(() => useChat());

      let sendPromise: Promise<void> = Promise.resolve();
      act(() => {
        sendPromise = result.current.sendMessage("привет", "model-x", "conv-1");
      });
      await waitFor(() => expect(result.current.isStreaming).toBe(true));

      act(() => {
        result.current.stop();
      });
      await act(async () => {
        await sendPromise;
      });

      // Advance well past 5 poll intervals (5×500ms) — old code would have fired
      // five 404s; the fix polls the result endpoint exactly once.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });

      const resultPolls = getSpy.mock.calls.filter(([url]) =>
        String(url).includes("/requests/"),
      );
      expect(resultPolls).toHaveLength(1);

      // …and reconciled to the server transcript: the optimistic user id became
      // the real one, and the orphaned (never-persisted) assistant bubble is gone.
      const userMsg = result.current.messages.find((m) => m.role === "user");
      expect(userMsg?.id).toBe("real-user-id");
      expect(
        result.current.messages.some((m) => m.role === "assistant"),
      ).toBe(false);
    } finally {
      vi.useRealTimers();
      getSpy.mockRestore();
    }
  });

  it("reconciles optimistic ids after a BROKEN (network) stream so resend doesn't 404 (tr-rydu)", async () => {
    // The first send streams, then the connection BREAKS mid-stream (a network
    // drop, NOT a Stop): the generator throws. The user message was persisted
    // before streaming, so the optimistic genIds must be reconciled to the real
    // transcript — otherwise a follow-up resend/edit hits /messages/{genId}/edit
    // → 404 and the message is silently lost (tr-rydu). Before the fix the
    // reconcile fired only on a Stop (aborted), never on a connection break.
    streamMock.mockImplementation(async function* (
      _conversationId: string,
      _content: unknown,
      _model: string,
      _token: string,
      _options: { signal: AbortSignal },
    ): AsyncGenerator<SSEEventPayload> {
      yield { event: "message_start", data: { request_id: "req-1", estimated_cost: "0" } };
      yield { event: "content_delta", data: { delta: "часть" } };
      throw new Error("network down");
    });

    vi.useFakeTimers({ shouldAdvanceTime: true });
    const getSpy = vi
      .spyOn(apiClient, "get")
      .mockImplementation(async (url: string) => {
        if (url.includes("/requests/")) {
          throw new ApiRequestError("Запрос не найден", 404, "not_found");
        }
        return {
          id: "conv-1",
          title: null,
          model_id: "model-x",
          created_at: "2026-06-17T00:00:00Z",
          updated_at: "2026-06-17T00:00:00Z",
          messages: [
            {
              id: "real-user-id",
              role: "user",
              content: "привет",
              model_id: null,
              input_tokens: null,
              output_tokens: null,
              cost_micro_rub: null,
              request_id: null,
              created_at: "2026-06-17T00:00:00Z",
            },
          ],
        } as unknown as ConversationResponse;
      });

    try {
      const { result } = renderHook(() => useChat());
      let sendPromise: Promise<void> = Promise.resolve();
      act(() => {
        sendPromise = result.current.sendMessage("привет", "model-x", "conv-1");
      });
      await waitFor(() => expect(result.current.isStreaming).toBe(true));
      await act(async () => {
        await sendPromise;
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });

      // Reconcile fired on the connection break → optimistic user id swapped for
      // the real one, so a subsequent resend/edit targets a real row (no 404).
      const userMsg = result.current.messages.find((m) => m.role === "user");
      expect(userMsg?.id).toBe("real-user-id");
    } finally {
      vi.useRealTimers();
      getSpy.mockRestore();
    }
  });

  it("still retries a transient 5xx before giving up", async () => {
    // A transient/5xx is NOT terminal: the partial may not be persisted YET, so a
    // short retry is reasonable. First poll 503s, the second returns the result.
    streamMock.mockImplementation(blockingStreamWithStart());

    vi.useFakeTimers({ shouldAdvanceTime: true });
    const getSpy = vi
      .spyOn(apiClient, "get")
      .mockRejectedValueOnce(
        new ApiRequestError("Сервис недоступен", 503, "service_unavailable"),
      )
      .mockResolvedValueOnce({
        request_id: "req-1",
        message_id: "real-asst-id",
        user_message_id: "user-1",
        active_leaf_id: "real-asst-id",
        actual_cost: "3.5974",
        balance_after: "100.00",
        tokens_used: { input: 3707, output: 2038, reasoning: 0 },
      } satisfies SSEMessageDone);

    try {
      const { result } = renderHook(() => useChat());

      let sendPromise: Promise<void> = Promise.resolve();
      act(() => {
        sendPromise = result.current.sendMessage("привет", "model-x", "conv-1");
      });
      await waitFor(() => expect(result.current.isStreaming).toBe(true));

      act(() => {
        result.current.stop();
      });
      await act(async () => {
        await sendPromise;
      });

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });

      // Two polls: the 503 retried, then the 200 reconciled the bubble.
      expect(getSpy).toHaveBeenCalledTimes(2);
      const asst = result.current.messages.find((m) => m.role === "assistant");
      expect(asst?.id).toBe("real-asst-id");
      expect(asst?.actualCost).toBe("3.5974");
    } finally {
      vi.useRealTimers();
      getSpy.mockRestore();
    }
  });
});
