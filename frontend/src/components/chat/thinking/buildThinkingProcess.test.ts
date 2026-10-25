import { describe, it, expect } from "vitest";
import { buildThinkingProcess } from "./buildThinkingProcess";

describe("buildThinkingProcess", () => {
  it("returns no segments for empty input", () => {
    expect(buildThinkingProcess({}).segments).toEqual([]);
  });

  it("maps reasoning text to a single reasoning segment", () => {
    const p = buildThinkingProcess({ thinking: "hello" });
    expect(p.segments).toEqual([
      { kind: "reasoning", id: "reasoning", text: "hello" },
    ]);
  });

  it("ignores whitespace-only reasoning text", () => {
    expect(buildThinkingProcess({ thinking: "   " }).segments).toEqual([]);
  });

  it("appends tool steps after reasoning", () => {
    const p = buildThinkingProcess({
      thinking: "x",
      thinkingSteps: [{ id: "s1", title: "T", tool: "script", status: "done" }],
    });
    expect(p.segments.map((s) => s.kind)).toEqual(["reasoning", "tool_call"]);
  });

  it("carries run_python metadata (duration/stderr/truncated) onto the tool_call segment", () => {
    const p = buildThinkingProcess({
      thinkingSteps: [
        {
          id: "tc-0-0",
          title: "Выполнение Python",
          tool: "run_python",
          status: "done",
          output: "ok",
          duration_ms: 139,
          stderr: "warn",
          truncated: true,
        },
      ],
    });
    const seg = p.segments.find((s) => s.kind === "tool_call");
    expect(seg).toMatchObject({ duration_ms: 139, stderr: "warn", truncated: true });
  });

  it("passes through active flag, timing and tokens", () => {
    const p = buildThinkingProcess({
      isThinking: true,
      thinkingStartedAt: 1000,
      thinkingDurationMs: 6000,
      reasoningTokens: 1200,
    });
    expect(p.isActive).toBe(true);
    expect(p.startedAt).toBe(1000);
    expect(p.durationMs).toBe(6000);
    expect(p.reasoningTokens).toBe(1200);
  });
});

describe("buildThinkingProcess — multi-segment (tr-cy0.3.9)", () => {
  it("builds one reasoning segment per accumulated segment with stable ids", () => {
    const p = buildThinkingProcess({
      thinkingSegments: [
        { id: "seg-0", text: "Анализ", summary: "Анализирую" },
        { id: "seg-1", text: "Ответ", summaryPending: true },
      ],
    });
    expect(p.segments).toEqual([
      { kind: "reasoning", id: "seg-0", text: "Анализ", summary: "Анализирую" },
      { kind: "reasoning", id: "seg-1", text: "Ответ", summaryPending: true },
    ]);
  });

  it("prefers thinkingSegments over the flat thinking fallback", () => {
    const p = buildThinkingProcess({
      thinking: "flat legacy text",
      thinkingSegments: [{ id: "seg-0", text: "structured" }],
    });
    expect(p.segments).toEqual([
      { kind: "reasoning", id: "seg-0", text: "structured" },
    ]);
  });

  it("falls back to one segment from flat thinking when segments empty", () => {
    expect(
      buildThinkingProcess({ thinking: "hello", thinkingSegments: [] }).segments,
    ).toEqual([{ kind: "reasoning", id: "reasoning", text: "hello" }]);
  });

  it("filters empty-text segments in the multi-segment path (tr-cy0.3.14)", () => {
    const p = buildThinkingProcess({
      thinkingSegments: [
        { id: "seg-0", text: "real", summary: "T0" },
        { id: "seg-1", text: "   ", summary: "T1" },
      ],
    });
    expect(p.segments.map((s) => s.id)).toEqual(["seg-0"]);
  });

  it("appends tool steps after multiple reasoning segments, deterministic order", () => {
    const p = buildThinkingProcess({
      thinkingSegments: [
        { id: "seg-0", text: "a" },
        { id: "seg-1", text: "b" },
      ],
      thinkingSteps: [{ id: "s1", title: "T", tool: "script", status: "done" }],
    });
    expect(p.segments.map((s) => [s.kind, s.id])).toEqual([
      ["reasoning", "seg-0"],
      ["reasoning", "seg-1"],
      ["tool_call", "s1"],
    ]);
  });
});

describe("buildThinkingProcess — tool-step fields (tr-cy0.2.10)", () => {
  it("carries filename/diff/presented from thinkingSteps to tool_call segments", () => {
    const p = buildThinkingProcess({
      thinkingSteps: [
        {
          id: "tc-0-0", title: "Правка файла a.md", tool: "str_replace",
          status: "done", filename: "a.md", diff: { added: 5, removed: 1 },
          presented: false,
        },
      ],
    });
    const seg = p.segments.find((s) => s.kind === "tool_call");
    expect(seg).toMatchObject({
      kind: "tool_call", filename: "a.md", diff: { added: 5, removed: 1 },
    });
  });
});
