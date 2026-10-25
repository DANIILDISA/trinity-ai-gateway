import type {
  ThinkingProcess,
  ThinkingSegment,
  ThinkingStep,
} from "@/types/thinking";

// Structural subset of ChatMessage the selector reads — keeps the selector
// decoupled from the chat hook.
export interface ThinkingSource {
  thinking?: string;
  // Ordered reasoning segments (multi-segment timeline, tr-cy0.3.8/3.9). When
  // present and non-empty these drive the segments; otherwise the flat `thinking`
  // is the single-segment fallback.
  thinkingSegments?: {
    id: string;
    text: string;
    summary?: string;
    summaryPending?: boolean;
  }[];
  isThinking?: boolean;
  thinkingStartedAt?: number;
  thinkingDurationMs?: number;
  reasoningTokens?: number;
  thinkingSteps?: ThinkingStep[];
  // Whether this turn was stopped mid-reply (tr-fo1) — drives the «Остановлено»
  // terminal node instead of «Готово». Read-path derived; absent ⇒ completed.
  stopped?: boolean;
}

export function buildThinkingProcess(source: ThinkingSource): ThinkingProcess {
  const segments: ThinkingSegment[] = [];

  // Drop empty-text segments so a blank/placeholder segment never renders
  // (tr-cy0.3.14) — mirrors the legacy `text.trim().length > 0` guard below.
  const reasoningSegments = (source.thinkingSegments ?? []).filter(
    (s) => s.text.trim().length > 0,
  );
  if (reasoningSegments.length > 0) {
    // Multi-segment timeline: one reasoning segment per accumulated/persisted
    // segment, each carrying its own STABLE id and one-line title.
    for (const s of reasoningSegments) {
      segments.push({
        kind: "reasoning",
        id: s.id,
        text: s.text,
        summary: s.summary,
        summaryPending: s.summaryPending,
      });
    }
  } else {
    const text = source.thinking ?? "";
    // Legacy single block: a fixed id is safe (no interleaving with steps).
    if (text.trim().length > 0) {
      segments.push({ kind: "reasoning", id: "reasoning", text });
    }
  }

  // Tool steps follow the reasoning segments. Interleaving reasoning with tool
  // steps by timestamp is future work (the step kit is still largely dormant);
  // the order here stays deterministic and matches the prior single-segment output.
  for (const step of source.thinkingSteps ?? []) {
    segments.push({
      kind: "tool_call",
      id: step.id,
      title: step.title,
      tool: step.tool,
      input: step.input,
      output: step.output,
      status: step.status,
      // Carry the reference tool-block fields through to ToolStepSegment.
      filename: step.filename,
      diff: step.diff,
      presented: step.presented,
      // run_python run-card metadata (tr-7so1).
      stderr: step.stderr,
      duration_ms: step.duration_ms,
      truncated: step.truncated,
      // Rich outputs persisted for reload (Phase 4 tr-5ktl).
      results: step.results ?? undefined,
    });
  }

  return {
    segments,
    isActive: !!source.isThinking,
    startedAt: source.thinkingStartedAt,
    durationMs: source.thinkingDurationMs,
    reasoningTokens: source.reasoningTokens,
    stopped: !!source.stopped,
  };
}
