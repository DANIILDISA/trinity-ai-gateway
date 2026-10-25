// Shared SSE-stream accumulator for the chat hook (tr-sja). Both sendMessage and
// runActionStream (regenerate/edit) stream the SAME content/thinking/segment/
// tool-step events and accumulate them off-React into mutable buffers, flushing
// a snapshot to state on a fixed cadence (BUG 6 / tr-1hm, which originally
// duplicated ~150 lines of this between the two handlers). The buffer mutations
// are identical; the two handlers differ ONLY in their side-effects on the first
// reasoning event and the first answer token — those are injected via `hooks`.
// This module owns the common accumulation; the callers own message_start /
// message_done / error and the finally orchestration.

import type {
  SSEContentDelta,
  SSEEventPayload,
  SSEThinkingDelta,
  SSEThinkingSegment,
  SSEThinkingSummary,
  SSEToolCallStart,
  SSEToolCallArgumentsDelta,
  SSEToolResult,
} from "@/types/api";
import type { ThinkingStep } from "@/types/thinking";

// Live accumulator entry for a reasoning segment during streaming, mirrored to
// ChatMessage.thinkingSegments (tr-cy0.3.8). `summaryPending` is true on a
// freshly opened segment until its thinking_summary lands, so the header can
// show a placeholder rather than flashing the text-derived fallback.
export interface LiveThinkingSegment {
  id: string;
  text: string;
  summary?: string;
  summaryPending?: boolean;
}

// Mutable off-React buffers for one streaming assistant turn. `currentSegmentId`
// is the segment that new thinking_delta text appends to; the flat
// `pendingThinking` is kept in lockstep for the legacy single-segment fallback.
// `segmentsDirty`/`stepsDirty` gate whether a flush re-snapshots those arrays.
export interface StreamBuffers {
  pending: string;
  pendingThinking: string;
  liveSegments: LiveThinkingSegment[];
  currentSegmentId: string | null;
  segmentsDirty: boolean;
  liveSteps: ThinkingStep[];
  stepsDirty: boolean;
}

// The DIFFERING side-effects between the two handlers, injected by the caller.
// onThinkingStart fires on the first reasoning event of any kind (both handlers
// patch isThinking + thinkingStartedAt; sendMessage additionally tracks locals
// for the collapse + duration). onFirstContent fires on the first answer token —
// sendMessage collapses the thinking state with it; runActionStream omits it to
// preserve its current no-collapse behavior.
export interface StreamEventHooks {
  onThinkingStart?: () => void;
  onFirstContent?: () => void;
}

// A flush snapshot: only what changed since the last flush. `content`/`thinking`
// are the buffered deltas (empty string ⇒ nothing new, so the caller skips that
// field); `segments`/`steps` are present only when their dirty flag was set.
export interface BufferSnapshot {
  content: string;
  thinking: string;
  segments?: LiveThinkingSegment[];
  steps?: ThinkingStep[];
}

// The full drained state at message_done: all buffered text plus the complete
// segment/step arrays (null when none accumulated, so the finalize keeps
// whatever the bubble already holds).
export interface DrainedBuffers {
  content: string;
  thinking: string;
  segments: LiveThinkingSegment[] | null;
  steps: ThinkingStep[] | null;
}

export function createStreamBuffers(): StreamBuffers {
  return {
    pending: "",
    pendingThinking: "",
    liveSegments: [],
    currentSegmentId: null,
    segmentsDirty: false,
    liveSteps: [],
    stepsDirty: false,
  };
}

// Apply one SSE event to the buffers, invoking the caller's hooks for the
// differing side-effects. Returns true iff this was an accumulation event the
// module owns (content/thinking_*/tool_*); the caller still owns message_start,
// message_done, error and (sendMessage) context_truncated — for those this
// returns false so the caller can handle them.
export function applyStreamEvent(
  buffers: StreamBuffers,
  evt: SSEEventPayload,
  hooks: StreamEventHooks = {},
): boolean {
  switch (evt.event) {
    case "content_delta": {
      const data = evt.data as SSEContentDelta;
      if (data.delta) {
        buffers.pending += data.delta;
        // First answer token: sendMessage collapses the animated thinking state
        // here (runActionStream passes no hook → no collapse).
        hooks.onFirstContent?.();
      }
      return true;
    }
    case "thinking_delta": {
      const data = evt.data as SSEThinkingDelta;
      if (data.delta) {
        buffers.pendingThinking += data.delta;
        // Append to the current segment, lazily opening the implicit legacy
        // segment 0 when no thinking_segment preceded (its id must match the
        // backend's "seg-0" so a later thinking_summary binds).
        if (buffers.currentSegmentId === null) {
          buffers.liveSegments.push({ id: "seg-0", text: "", summaryPending: false });
          buffers.currentSegmentId = "seg-0";
        }
        const seg = buffers.liveSegments.find((s) => s.id === buffers.currentSegmentId);
        if (seg) seg.text += data.delta;
        buffers.segmentsDirty = true;
        hooks.onThinkingStart?.();
      }
      return true;
    }
    case "thinking_segment": {
      const data = evt.data as SSEThinkingSegment;
      // A real boundary: open a new segment. The backend always titles an
      // explicit segment, so mark the summary pending until it arrives.
      buffers.liveSegments.push({ id: data.segment_id, text: "", summaryPending: true });
      buffers.currentSegmentId = data.segment_id;
      buffers.segmentsDirty = true;
      hooks.onThinkingStart?.();
      return true;
    }
    case "thinking_summary": {
      const data = evt.data as SSEThinkingSummary;
      const seg = buffers.liveSegments.find((s) => s.id === data.segment_id);
      if (seg) {
        seg.summary = data.summary;
        seg.summaryPending = false;
        buffers.segmentsDirty = true;
      }
      return true;
    }
    case "tool_call_start": {
      const data = evt.data as SSEToolCallStart;
      buffers.liveSteps.push({
        id: data.step_id,
        tool: data.tool,
        title: data.title,
        status: "running",
      });
      buffers.stepsDirty = true;
      hooks.onThinkingStart?.();
      return true;
    }
    case "tool_call_arguments_delta": {
      const data = evt.data as SSEToolCallArgumentsDelta;
      const step = buffers.liveSteps.find((s) => s.id === data.step_id);
      if (step) {
        // Accumulate the streamed argument JSON so the chip can form live.
        step.input = ((step.input as string | undefined) ?? "") + data.delta;
        buffers.stepsDirty = true;
      }
      return true;
    }
    case "tool_result": {
      const data = evt.data as SSEToolResult;
      const step = buffers.liveSteps.find((s) => s.id === data.step_id);
      if (step) {
        step.status = data.status;
        if (data.filename != null) step.filename = data.filename;
        if (data.diff != null) step.diff = data.diff;
        if (data.presented != null) step.presented = data.presented;
        if (data.output != null) step.output = data.output;
        // run_python metadata (tr-pock) for the run-card.
        if (data.stderr != null) step.stderr = data.stderr;
        if (data.duration_ms != null) step.duration_ms = data.duration_ms;
        if (data.truncated != null) step.truncated = data.truncated;
        // Rich outputs (Phase 4 tr-5ktl): matplotlib PNGs, DataFrame HTML, text.
        if (data.results != null) step.results = data.results;
        buffers.stepsDirty = true;
      }
      return true;
    }
    case "tool_loop_halted": {
      // A hard bound stopped the loop — finalize any still-running step so it
      // never hangs pulsing (architecture §8). Marks dirty unconditionally,
      // mirroring the pre-refactor handler.
      for (const s of buffers.liveSteps) {
        if (s.status === "running") s.status = "error";
      }
      buffers.stepsDirty = true;
      return true;
    }
    default:
      return false;
  }
}

// Snapshot the buffers for a flush, resetting the buffered deltas + dirty flags.
// Returns null when there is nothing to flush (so the caller can skip setState).
// segments/steps are deep-copied only when dirty, so a later mutation of the
// live arrays never bleeds into an already-dispatched React update.
export function snapshotBuffers(buffers: StreamBuffers): BufferSnapshot | null {
  if (
    !buffers.pending &&
    !buffers.pendingThinking &&
    !buffers.segmentsDirty &&
    !buffers.stepsDirty
  ) {
    return null;
  }
  const snapshot: BufferSnapshot = {
    content: buffers.pending,
    thinking: buffers.pendingThinking,
    segments: buffers.segmentsDirty
      ? buffers.liveSegments.map((s) => ({ ...s }))
      : undefined,
    steps: buffers.stepsDirty ? buffers.liveSteps.map((s) => ({ ...s })) : undefined,
  };
  buffers.pending = "";
  buffers.pendingThinking = "";
  buffers.segmentsDirty = false;
  buffers.stepsDirty = false;
  return snapshot;
}

// Drain the full buffered state at message_done: all pending text plus the
// complete segment/step arrays (null when none), resetting the buffered deltas +
// dirty flags. The caller merges this into the bubble in the SAME update that
// finalizes it (applyMessageDone), so there's no flicker.
export function drainBuffers(buffers: StreamBuffers): DrainedBuffers {
  const drained: DrainedBuffers = {
    content: buffers.pending,
    thinking: buffers.pendingThinking,
    segments: buffers.liveSegments.length
      ? buffers.liveSegments.map((s) => ({ ...s }))
      : null,
    steps: buffers.liveSteps.length ? buffers.liveSteps.map((s) => ({ ...s })) : null,
  };
  buffers.pending = "";
  buffers.pendingThinking = "";
  buffers.segmentsDirty = false;
  buffers.stepsDirty = false;
  return drained;
}

// Coerce any still-running tool step to error (Stop/abort or an abnormal end can
// freeze a step mid-flight in `running`, pulsing forever next to a «Готово» rail
// — tr-3fh). Sets stepsDirty when it changed anything so the next flush
// reconciles the live bubble; returns whether it coerced.
export function coerceRunningStepsToError(buffers: StreamBuffers): boolean {
  let coerced = false;
  for (const s of buffers.liveSteps) {
    if (s.status === "running") {
      s.status = "error";
      coerced = true;
    }
  }
  if (coerced) buffers.stepsDirty = true;
  return coerced;
}
