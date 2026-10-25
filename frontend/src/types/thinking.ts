// The thinking process is an ordered list of typed segments rendered through the
// registry. New thinking capabilities are added by extending this union with a
// new `kind` and registering a renderer for it — nothing else changes.

export interface ReasoningSegment {
  kind: "reasoning";
  id: string;
  text: string;
  // Optional one-line summary title (Russian) shown in the collapsed state, in
  // the Claude.ai style. The provider gives no per-segment title (spike
  // tr-cy0.3.1), so it's produced from the segment text — see the chosen title
  // strategy. When absent the UI falls back to the first significant line of
  // `text`, i.e. it behaves exactly as today (graceful degradation).
  summary?: string;
  // True while the summary is still being produced (async/streaming) — lets the
  // header show a placeholder instead of flashing the fallback then the title.
  summaryPending?: boolean;
}

// A rich output captured in the sandbox (epic tr-s3y1 Phase 4, tr-5ktl). Tagged by
// MIME-ish `type`; the run-card dispatches a renderer per type. image/png carries the
// base64 `data` live and an `artifact_id`/`filename` once persisted (on reload only the
// artifact ref survives — the image is re-fetched). text/html|text/plain carry inline `data`.
// `file` (Phase 5, tr-an0g) is a generated workspace file: `data` is base64 live, dropped
// once persisted; `media_type` + `size` (decoded bytes) drive the download chip.
export interface RichResult {
  type: "image/png" | "text/html" | "text/plain" | "file";
  data?: string;
  artifact_id?: string;
  filename?: string;
  media_type?: string;
  size?: number;
  // Over-quota binary whose base64 was dropped from the row and has no artifact to
  // re-fetch (audit B4): the chip renders it disabled ("unavailable on reload").
  unavailable?: boolean;
}

export interface ToolCallSegment {
  kind: "tool_call";
  id: string;
  title: string; // e.g. "Создание файла python_exam_prep.md"
  tool: string; // function name, e.g. "create_file"
  input?: unknown;
  output?: string;
  status: "running" | "done" | "error";
  // Reference tool-block fields (tr-cy0.2): filename chip, inline diff counter,
  // and the «presented» marker.
  filename?: string;
  diff?: { added: number; removed: number };
  presented?: boolean;
  // Code-execution metadata (run_python, epic tr-s3y1 Phase 2): captured stderr,
  // wall-clock duration, and whether output was truncated — for the run-card.
  stderr?: string;
  duration_ms?: number;
  truncated?: boolean;
  // Rich outputs (Phase 4 tr-5ktl): matplotlib PNGs, DataFrame HTML tables, text.
  results?: RichResult[];
}

export type ThinkingSegment = ReasoningSegment | ToolCallSegment;

export interface ThinkingProcess {
  // Ordered list of potentially MULTIPLE reasoning segments interleaved with
  // tool_call segments (parity target: each reasoning segment renders its own
  // collapsible header titled from `summary`, falling back to the first
  // significant line of `text`). Today the backend emits a single flat reasoning
  // block; multi-segment streaming is wired up by the rest of the tr-cy0.3
  // cluster.
  segments: ThinkingSegment[];
  isActive: boolean; // model still thinking → auto-expand + pulse
  startedAt?: number; // live-clock anchor (epoch ms); live generation only
  durationMs?: number; // frozen duration when done; undefined on reloaded history
  // Reasoning-token count shown in the collapsed header. Audit fact: the live
  // header label is formed by formatThinking as «… токенов» — «токенов» is the
  // correct word. The «… планов» seen on an old screenshot is a stale-build
  // artifact, addressed by its own bug task (tr-cy0.3.13), not a label here.
  reasoningTokens?: number;
  // True when this turn was STOPPED mid-reply (usage_logs.status == 'partial',
  // surfaced read-path). The terminal rail node renders «Остановлено» instead of
  // «Готово» so a reloaded partial reply is distinguishable from a completed one
  // (tr-fo1). Falsy ⇒ the normal «Готово» node.
  stopped?: boolean;
}

// Backend-driven structured tool steps streamed into the timeline (tr-cy0.2).
export interface ThinkingStep {
  id: string;
  title: string;
  tool: string;
  input?: unknown;
  output?: string;
  status: "running" | "done" | "error";
  // Reference tool-block fields (tr-cy0.2): the filename chip, the inline diff
  // counter (+added/−removed), and whether the file was «presented» to the user.
  filename?: string;
  diff?: { added: number; removed: number };
  presented?: boolean;
  // Code-execution metadata (run_python, epic tr-s3y1 Phase 2).
  stderr?: string;
  duration_ms?: number;
  truncated?: boolean;
  // Rich outputs (Phase 4 tr-5ktl): live PNGs/DataFrame HTML/text; on reload images
  // carry only an artifact ref (re-fetched).
  results?: RichResult[];
}
