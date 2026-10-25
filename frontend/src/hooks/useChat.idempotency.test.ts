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

afterEach(() => {
  streamMock.mockReset();
});

// A normal one-shot send: message_done immediately so the stream completes.
function happyStream(): ReturnType<typeof vi.fn> {
  return vi.fn(
    // eslint-disable-next-line require-yield
    async function* (): AsyncGenerator<SSEEventPayload> {
      yield {
        event: "message_done",
        data: {
          request_id: "req-1",
          message_id: "asst-1",
          user_message_id: "user-1",
          active_leaf_id: "asst-1",
          actual_cost: "0.50",
          balance_after: "99.50",
          tokens_used: { input: 3, output: 1, reasoning: 0 },
        },
      };
    },
  );
}

describe("useChat — backend idempotency plumbing (tr-n5b)", () => {
  it("passes a fresh UUID client_request_id to streamChatMessage on each send", async () => {
    streamMock.mockImplementation(happyStream());
    const { result } = renderHook(() => useChat());

    await act(async () => {
      await result.current.sendMessage("привет", "model-x", "conv-1");
    });

    await waitFor(() => expect(streamMock).toHaveBeenCalledTimes(1));
    const options1 = streamMock.mock.calls[0][4] as { clientRequestId?: string };
    expect(options1.clientRequestId).toBeDefined();
    // Must be a UUID (crypto.randomUUID), not an arbitrary string.
    expect(options1.clientRequestId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
    );

    // A SECOND send attempt gets a DIFFERENT id (one submit ⇒ one id).
    await act(async () => {
      await result.current.sendMessage("ещё", "model-x", "conv-1");
    });
    await waitFor(() => expect(streamMock).toHaveBeenCalledTimes(2));
    const options2 = streamMock.mock.calls[1][4] as { clientRequestId?: string };
    expect(options2.clientRequestId).toBeDefined();
    expect(options2.clientRequestId).not.toBe(options1.clientRequestId);
  });

  it("sets no error state when the SSE layer silently ends a duplicate send", async () => {
    // streamSse swallows a duplicate_request 409 by returning with NO error
    // event (sse.ts). The hook must therefore leave `error` null and not mark the
    // bubble errored — the original turn already represents the send.
    streamMock.mockImplementation(
      // eslint-disable-next-line require-yield
      async function* (): AsyncGenerator<SSEEventPayload> {
        // No events at all — mirrors the swallowed 409 path.
      },
    );
    const { result } = renderHook(() => useChat());

    await act(async () => {
      await result.current.sendMessage("дубль", "model-x", "conv-1");
    });

    expect(result.current.error).toBeNull();
    const asst = result.current.messages.find((m) => m.role === "assistant");
    expect(asst?.errorMessage).toBeUndefined();
  });
});
