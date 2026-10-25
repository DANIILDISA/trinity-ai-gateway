import { afterEach, describe, expect, it, vi } from "vitest";
import { streamChatMessage, streamEdit, streamRegenerate } from "@/lib/sse";
import type { SSEEventPayload } from "@/types/api";

function sseStream(frames: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(c) {
      for (const f of frames) c.enqueue(enc.encode(f));
      c.close();
    },
  });
}

function mockOkResponse(frames: string[]): Response {
  return {
    ok: true,
    status: 200,
    body: sseStream(frames),
    text: async () => "",
  } as unknown as Response;
}

async function collect(
  gen: AsyncGenerator<SSEEventPayload>,
): Promise<SSEEventPayload[]> {
  const out: SSEEventPayload[] = [];
  for await (const e of gen) out.push(e);
  return out;
}

afterEach(() => {
  vi.restoreAllMocks();
  document.cookie = "csrf_token=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
});

const DONE_FRAME =
  "event: message_done\n" +
  'data: {"request_id":"r1","actual_cost":"0.50 ₽","balance_after":"99 ₽",' +
  '"tokens_used":{"input":10,"output":5},' +
  '"message_id":"new-asst-id","active_leaf_id":"new-asst-id"}\n\n';

describe("streamRegenerate", () => {
  it("POSTs to the regenerate URL with overrides and forwards CSRF", async () => {
    document.cookie = "csrf_token=tok123";
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(mockOkResponse([DONE_FRAME]));

    const events = await collect(
      streamRegenerate("conv-1", "asst-1", "tok-jwt", {
        baseUrl: "http://api.test",
        modelId: "gpt-x",
        thinkingEnabled: true,
        thinkingEffort: "high",
        webSearchEnabled: true,
      }),
    );

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "http://api.test/api/v1/chat/conversations/conv-1/messages/asst-1/regenerate",
    );
    expect((init!.headers as Record<string, string>)["X-CSRF-Token"]).toBe(
      "tok123",
    );
    expect(JSON.parse(init!.body as string)).toEqual({
      model: "gpt-x",
      thinking_enabled: true,
      thinking_effort: "high",
      web_search_enabled: true,
    });
    const done = events.find((e) => e.event === "message_done");
    expect(done?.data).toMatchObject({
      message_id: "new-asst-id",
      active_leaf_id: "new-asst-id",
    });
  });

  it("omits override fields that are undefined", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(mockOkResponse([DONE_FRAME]));

    await collect(
      streamRegenerate("conv-1", "asst-1", "tok-jwt", {
        baseUrl: "http://api.test",
        modelId: "gpt-x",
      }),
    );

    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(init!.body as string)).toEqual({ model: "gpt-x" });
  });
});

describe("streamEdit", () => {
  it("POSTs to the edit URL with the new content", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(mockOkResponse([DONE_FRAME]));

    await collect(
      streamEdit("conv-1", "user-1", "правленый текст", "tok-jwt", {
        baseUrl: "http://api.test",
      }),
    );

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "http://api.test/api/v1/chat/conversations/conv-1/messages/user-1/edit",
    );
    expect(JSON.parse(init!.body as string)).toEqual({
      content: "правленый текст",
    });
  });
});

describe("streamChatMessage (unchanged contract)", () => {
  it("still POSTs to the messages URL", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(mockOkResponse([DONE_FRAME]));

    await collect(
      streamChatMessage("conv-1", "привет", "gpt-x", "tok-jwt", {
        baseUrl: "http://api.test",
      }),
    );

    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://api.test/api/v1/chat/conversations/conv-1/messages",
    );
  });
});

describe("streamChatMessage idempotency (tr-n5b)", () => {
  it("includes client_request_id in the POST body when provided", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(mockOkResponse([DONE_FRAME]));

    await collect(
      streamChatMessage("conv-1", "привет", "gpt-x", "tok-jwt", {
        baseUrl: "http://api.test",
        clientRequestId: "11111111-2222-3333-4444-555555555555",
      }),
    );

    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(init!.body as string).client_request_id).toBe(
      "11111111-2222-3333-4444-555555555555",
    );
  });

  it("swallows a duplicate_request 409 silently — no error event", async () => {
    // The backend idempotency gate rejects a duplicate of the SAME logical send
    // with 409 duplicate_request. The frontend submit lock already prevents the
    // common case, so this 409 is a benign "already in flight/sent": end the
    // stream with NO error event so the existing error UI shows nothing scary.
    const body = JSON.stringify({
      error: {
        code: "duplicate_request",
        message: "Запрос уже обрабатывается",
        details: null,
      },
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false,
      status: 409,
      body: null,
      text: async () => body,
    } as unknown as Response);

    const events = await collect(
      streamChatMessage("conv-1", "дубль", "gpt-x", "tok-jwt", {
        clientRequestId: "11111111-2222-3333-4444-555555555555",
      }),
    );
    expect(events.some((e) => e.event === "error")).toBe(false);
    expect(events).toHaveLength(0);
  });
});

// Compile-time contract (tr-cy0.3.3): SSEEventPayload MUST carry the
// thinking_segment / thinking_summary variants with these exact data shapes. If
// a variant is dropped from the union, Extract<…> collapses to `never` and
// assigning a concrete payload to it is a `tsc` error — so `npx tsc --noEmit`
// is the real gate for this contract (vitest does not typecheck).
const _segmentContract: Extract<
  SSEEventPayload,
  { event: "thinking_segment" }
> = { event: "thinking_segment", data: { segment_id: "s", index: 0 } };
const _summaryContract: Extract<
  SSEEventPayload,
  { event: "thinking_summary" }
> = { event: "thinking_summary", data: { segment_id: "s", summary: "т" } };
const _toolStartContract: Extract<
  SSEEventPayload,
  { event: "tool_call_start" }
> = { event: "tool_call_start", data: { step_id: "tc-0-0", tool: "create_file", title: "t" } };
const _toolResultContract: Extract<
  SSEEventPayload,
  { event: "tool_result" }
> = {
  event: "tool_result",
  data: { step_id: "tc-0-0", status: "done", diff: { added: 1, removed: 0 } },
};
void _segmentContract;
void _summaryContract;
void _toolStartContract;
void _toolResultContract;

describe("non-OK response error mapping (tr-5zt)", () => {
  it("maps a 422 validation body to details[].msg, never raw JSON or the echoed input", async () => {
    const longInput = "x".repeat(99000);
    const body = JSON.stringify({
      error: {
        code: "validation_error",
        message: "Ошибка валидации данных",
        details: [
          {
            type: "content_length",
            loc: ["body", "content"],
            msg: "Сообщение должно содержать от 1 до 32000 символов",
            input: longInput,
          },
        ],
      },
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false,
      status: 422,
      body: null,
      text: async () => body,
    } as unknown as Response);

    const events = await collect(streamChatMessage("c1", longInput, "m", "tok"));
    const err = events.find((e) => e.event === "error");
    const data = err?.data as { code: string; message: string };
    expect(data.message).toBe(
      "Сообщение должно содержать от 1 до 32000 символов",
    );
    expect(data.message).not.toContain("{");
    expect(data.message).not.toContain(longInput);
    // Sentinel code so the consumer's getErrorMessage falls through to our
    // already-localized message rather than the generic envelope text.
    expect(data.code).toBe("http_error");
  });
});

describe("thinking_segment / thinking_summary SSE contract (tr-cy0.3.3)", () => {
  it("parses both events into their typed payloads", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      mockOkResponse([
        'event: thinking_segment\ndata: {"segment_id":"s0","index":0}\n\n',
        'event: thinking_delta\ndata: {"delta":"hmm"}\n\n',
        'event: thinking_summary\ndata: {"segment_id":"s0","summary":"Разбираю вопросы"}\n\n',
        DONE_FRAME,
      ]),
    );

    const events = await collect(streamChatMessage("c1", "hi", "m", "tok"));

    const seg = events.find((e) => e.event === "thinking_segment");
    expect(seg?.data).toEqual({ segment_id: "s0", index: 0 });

    const sum = events.find((e) => e.event === "thinking_summary");
    expect(sum?.data).toEqual({ segment_id: "s0", summary: "Разбираю вопросы" });
  });

  it("parses the agentic tool events into typed payloads (tr-cy0.2.6)", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      mockOkResponse([
        'event: tool_call_start\ndata: {"step_id":"tc-0-0","tool":"create_file","title":"Создание файла a.md"}\n\n',
        'event: tool_call_arguments_delta\ndata: {"step_id":"tc-0-0","delta":"{\\"path\\":"}\n\n',
        'event: tool_result\ndata: {"step_id":"tc-0-0","status":"done","filename":"a.md","diff":{"added":2,"removed":0},"presented":true}\n\n',
        'event: tool_loop_halted\ndata: {"reason":"max_rounds"}\n\n',
        DONE_FRAME,
      ]),
    );
    const events = await collect(streamChatMessage("c1", "hi", "m", "tok"));

    expect(events.find((e) => e.event === "tool_call_start")?.data).toEqual({
      step_id: "tc-0-0",
      tool: "create_file",
      title: "Создание файла a.md",
    });
    expect(events.find((e) => e.event === "tool_result")?.data).toMatchObject({
      status: "done",
      filename: "a.md",
      diff: { added: 2, removed: 0 },
      presented: true,
    });
    expect(events.find((e) => e.event === "tool_loop_halted")?.data).toEqual({
      reason: "max_rounds",
    });
  });

  it("treats a thinking_delta-only stream as the legacy single segment", async () => {
    // No thinking_segment frame at all → the existing flat thinking_delta path
    // is untouched (segment index 0 implied by the consumer).
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      mockOkResponse([
        'event: thinking_delta\ndata: {"delta":"legacy"}\n\n',
        DONE_FRAME,
      ]),
    );

    const events = await collect(streamChatMessage("c1", "hi", "m", "tok"));
    expect(events.some((e) => e.event === "thinking_segment")).toBe(false);
    const delta = events.find((e) => e.event === "thinking_delta");
    expect(delta?.data).toEqual({ delta: "legacy" });
  });
});
