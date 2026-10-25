import { describe, it, expect, vi } from "vitest";

import {
  applyStreamEvent,
  coerceRunningStepsToError,
  createStreamBuffers,
  drainBuffers,
  snapshotBuffers,
} from "./streamAccumulator";
import type { SSEEventPayload } from "@/types/api";

describe("streamAccumulator — createStreamBuffers", () => {
  it("returns fresh zeroed buffers", () => {
    expect(createStreamBuffers()).toEqual({
      pending: "",
      pendingThinking: "",
      liveSegments: [],
      currentSegmentId: null,
      segmentsDirty: false,
      liveSteps: [],
      stepsDirty: false,
    });
  });

  it("returns an independent instance each call", () => {
    const a = createStreamBuffers();
    const b = createStreamBuffers();
    a.liveSegments.push({ id: "x", text: "" });
    expect(b.liveSegments).toHaveLength(0);
  });
});

describe("streamAccumulator — applyStreamEvent: run_python metadata (tr-pock)", () => {
  it("carries duration_ms/stderr/truncated from tool_result onto the step", () => {
    const b = createStreamBuffers();
    b.liveSteps.push({
      id: "tc-0-0",
      tool: "run_python",
      title: "Выполнение Python",
      status: "running",
    });
    const handled = applyStreamEvent(b, {
      event: "tool_result",
      data: {
        step_id: "tc-0-0",
        status: "error",
        output: "boom",
        stderr: "Traceback (most recent call last)",
        duration_ms: 1234,
        truncated: true,
      },
    } as SSEEventPayload);
    expect(handled).toBe(true);
    const step = b.liveSteps.find((s) => s.id === "tc-0-0");
    expect(step?.status).toBe("error");
    expect(step?.duration_ms).toBe(1234);
    expect(step?.stderr).toBe("Traceback (most recent call last)");
    expect(step?.truncated).toBe(true);
  });
});

describe("streamAccumulator — applyStreamEvent: content", () => {
  it("accumulates a content_delta and fires onFirstContent (handled=true)", () => {
    const b = createStreamBuffers();
    const onFirstContent = vi.fn();
    const handled = applyStreamEvent(
      b,
      { event: "content_delta", data: { delta: "Hi" } },
      { onFirstContent },
    );
    expect(handled).toBe(true);
    expect(b.pending).toBe("Hi");
    expect(onFirstContent).toHaveBeenCalledTimes(1);
  });

  it("ignores an empty content_delta (no pending change, no hook) but reports handled", () => {
    const b = createStreamBuffers();
    const onFirstContent = vi.fn();
    const handled = applyStreamEvent(
      b,
      { event: "content_delta", data: { delta: "" } },
      { onFirstContent },
    );
    expect(handled).toBe(true);
    expect(b.pending).toBe("");
    expect(onFirstContent).not.toHaveBeenCalled();
  });

  it("works with no hooks supplied", () => {
    const b = createStreamBuffers();
    expect(applyStreamEvent(b, { event: "content_delta", data: { delta: "x" } })).toBe(true);
    expect(b.pending).toBe("x");
  });
});

describe("streamAccumulator — applyStreamEvent: thinking", () => {
  it("opens implicit seg-0 on a flat thinking_delta and fires onThinkingStart", () => {
    const b = createStreamBuffers();
    const onThinkingStart = vi.fn();
    applyStreamEvent(b, { event: "thinking_delta", data: { delta: "Think" } }, { onThinkingStart });
    expect(b.liveSegments).toEqual([{ id: "seg-0", text: "Think", summaryPending: false }]);
    expect(b.currentSegmentId).toBe("seg-0");
    expect(b.pendingThinking).toBe("Think");
    expect(b.segmentsDirty).toBe(true);
    expect(onThinkingStart).toHaveBeenCalledTimes(1);
  });

  it("ignores an empty thinking_delta", () => {
    const b = createStreamBuffers();
    const onThinkingStart = vi.fn();
    applyStreamEvent(b, { event: "thinking_delta", data: { delta: "" } }, { onThinkingStart });
    expect(b.liveSegments).toHaveLength(0);
    expect(b.pendingThinking).toBe("");
    expect(onThinkingStart).not.toHaveBeenCalled();
  });

  it("opens a titled segment on thinking_segment (summaryPending) and switches current", () => {
    const b = createStreamBuffers();
    applyStreamEvent(b, { event: "thinking_delta", data: { delta: "A" } });
    applyStreamEvent(b, { event: "thinking_segment", data: { segment_id: "seg-1", index: 1 } });
    applyStreamEvent(b, { event: "thinking_delta", data: { delta: "B" } });
    expect(b.liveSegments).toEqual([
      { id: "seg-0", text: "A", summaryPending: false },
      { id: "seg-1", text: "B", summaryPending: true },
    ]);
    expect(b.currentSegmentId).toBe("seg-1");
  });

  it("binds a summary to its segment and clears summaryPending", () => {
    const b = createStreamBuffers();
    applyStreamEvent(b, { event: "thinking_segment", data: { segment_id: "seg-1", index: 1 } });
    applyStreamEvent(b, { event: "thinking_summary", data: { segment_id: "seg-1", summary: "Готовлю" } });
    expect(b.liveSegments[0]).toEqual({
      id: "seg-1",
      text: "",
      summary: "Готовлю",
      summaryPending: false,
    });
  });

  it("ignores a thinking_summary for an unknown segment (still handled)", () => {
    const b = createStreamBuffers();
    const handled = applyStreamEvent(b, {
      event: "thinking_summary",
      data: { segment_id: "ghost", summary: "x" },
    });
    expect(handled).toBe(true);
    expect(b.liveSegments).toHaveLength(0);
  });
});

describe("streamAccumulator — applyStreamEvent: tool steps", () => {
  it("accumulates a tool step start → args → result and fires onThinkingStart once", () => {
    const b = createStreamBuffers();
    const onThinkingStart = vi.fn();
    applyStreamEvent(
      b,
      { event: "tool_call_start", data: { step_id: "tc-0-0", tool: "create_file", title: "T" } },
      { onThinkingStart },
    );
    applyStreamEvent(b, {
      event: "tool_call_arguments_delta",
      data: { step_id: "tc-0-0", delta: '{"a":1}' },
    });
    applyStreamEvent(b, {
      event: "tool_result",
      data: {
        step_id: "tc-0-0",
        status: "done",
        filename: "a.md",
        diff: { added: 2, removed: 0 },
        presented: true,
        output: "ok",
      },
    });
    expect(onThinkingStart).toHaveBeenCalledTimes(1);
    expect(b.liveSteps).toHaveLength(1);
    expect(b.liveSteps[0]).toMatchObject({
      id: "tc-0-0",
      tool: "create_file",
      title: "T",
      status: "done",
      input: '{"a":1}',
      filename: "a.md",
      diff: { added: 2, removed: 0 },
      presented: true,
      output: "ok",
    });
    expect(b.stepsDirty).toBe(true);
  });

  it("treats tool_call_arguments_delta for an unknown step as a no-op", () => {
    const b = createStreamBuffers();
    const handled = applyStreamEvent(b, {
      event: "tool_call_arguments_delta",
      data: { step_id: "ghost", delta: "x" },
    });
    expect(handled).toBe(true);
    expect(b.liveSteps).toHaveLength(0);
  });

  it("coerces running steps to error on tool_loop_halted and marks dirty unconditionally", () => {
    const b = createStreamBuffers();
    applyStreamEvent(b, {
      event: "tool_call_start",
      data: { step_id: "tc-0-0", tool: "x", title: "t" },
    });
    b.stepsDirty = false; // observe the halt re-setting it
    applyStreamEvent(b, { event: "tool_loop_halted", data: { reason: "max_rounds" } });
    expect(b.liveSteps[0].status).toBe("error");
    expect(b.stepsDirty).toBe(true);
  });
});

describe("streamAccumulator — applyStreamEvent: caller-owned events", () => {
  it("returns false for events the caller owns", () => {
    const b = createStreamBuffers();
    for (const event of [
      "message_start",
      "message_done",
      "error",
      "context_truncated",
      "some_future_event",
    ]) {
      expect(
        applyStreamEvent(b, { event, data: {} } as unknown as SSEEventPayload),
      ).toBe(false);
    }
    // None of those touched the buffers.
    expect(b).toEqual(createStreamBuffers());
  });
});

describe("streamAccumulator — snapshotBuffers", () => {
  it("returns null when nothing is buffered or dirty", () => {
    expect(snapshotBuffers(createStreamBuffers())).toBeNull();
  });

  it("snapshots content + thinking + dirty segments/steps and resets them", () => {
    const b = createStreamBuffers();
    applyStreamEvent(b, { event: "content_delta", data: { delta: "Hi" } });
    applyStreamEvent(b, { event: "thinking_delta", data: { delta: "Th" } });
    applyStreamEvent(b, {
      event: "tool_call_start",
      data: { step_id: "s", tool: "t", title: "T" },
    });
    const snap = snapshotBuffers(b);
    expect(snap).not.toBeNull();
    expect(snap!.content).toBe("Hi");
    expect(snap!.thinking).toBe("Th");
    expect(snap!.segments).toEqual([{ id: "seg-0", text: "Th", summaryPending: false }]);
    expect(snap!.steps).toEqual([{ id: "s", tool: "t", title: "T", status: "running" }]);
    expect(b.pending).toBe("");
    expect(b.pendingThinking).toBe("");
    expect(b.segmentsDirty).toBe(false);
    expect(b.stepsDirty).toBe(false);
  });

  it("omits segments/steps that are not dirty", () => {
    const b = createStreamBuffers();
    applyStreamEvent(b, { event: "content_delta", data: { delta: "x" } });
    const snap = snapshotBuffers(b);
    expect(snap!.content).toBe("x");
    expect(snap!.segments).toBeUndefined();
    expect(snap!.steps).toBeUndefined();
  });

  it("deep-copies segments so later mutation does not bleed into the snapshot", () => {
    const b = createStreamBuffers();
    applyStreamEvent(b, { event: "thinking_delta", data: { delta: "A" } });
    const snap = snapshotBuffers(b);
    applyStreamEvent(b, { event: "thinking_delta", data: { delta: "B" } });
    expect(snap!.segments).toEqual([{ id: "seg-0", text: "A", summaryPending: false }]);
    expect(b.liveSegments[0].text).toBe("AB");
  });
});

describe("streamAccumulator — drainBuffers", () => {
  it("returns full content/thinking/segments/steps and resets pending + dirty", () => {
    const b = createStreamBuffers();
    applyStreamEvent(b, { event: "content_delta", data: { delta: "ans" } });
    applyStreamEvent(b, { event: "thinking_delta", data: { delta: "th" } });
    applyStreamEvent(b, {
      event: "tool_call_start",
      data: { step_id: "s", tool: "t", title: "T" },
    });
    const d = drainBuffers(b);
    expect(d.content).toBe("ans");
    expect(d.thinking).toBe("th");
    expect(d.segments).toEqual([{ id: "seg-0", text: "th", summaryPending: false }]);
    expect(d.steps).toEqual([{ id: "s", tool: "t", title: "T", status: "running" }]);
    expect(b.pending).toBe("");
    expect(b.pendingThinking).toBe("");
    expect(b.segmentsDirty).toBe(false);
    expect(b.stepsDirty).toBe(false);
  });

  it("returns null segments/steps when none were accumulated", () => {
    const b = createStreamBuffers();
    applyStreamEvent(b, { event: "content_delta", data: { delta: "x" } });
    const d = drainBuffers(b);
    expect(d.content).toBe("x");
    expect(d.thinking).toBe("");
    expect(d.segments).toBeNull();
    expect(d.steps).toBeNull();
  });
});

describe("streamAccumulator — coerceRunningStepsToError", () => {
  it("coerces running steps to error, marks dirty, returns true", () => {
    const b = createStreamBuffers();
    applyStreamEvent(b, {
      event: "tool_call_start",
      data: { step_id: "s", tool: "t", title: "T" },
    });
    b.stepsDirty = false;
    expect(coerceRunningStepsToError(b)).toBe(true);
    expect(b.liveSteps[0].status).toBe("error");
    expect(b.stepsDirty).toBe(true);
  });

  it("returns false and leaves stepsDirty untouched when nothing is running", () => {
    const b = createStreamBuffers();
    applyStreamEvent(b, {
      event: "tool_call_start",
      data: { step_id: "s", tool: "t", title: "T" },
    });
    applyStreamEvent(b, { event: "tool_result", data: { step_id: "s", status: "done" } });
    b.stepsDirty = false;
    expect(coerceRunningStepsToError(b)).toBe(false);
    expect(b.liveSteps[0].status).toBe("done");
    expect(b.stepsDirty).toBe(false);
  });
});
