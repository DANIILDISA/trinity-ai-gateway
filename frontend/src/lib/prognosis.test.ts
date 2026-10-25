import { describe, it, expect } from "vitest";
import { estimatePrognosis } from "./prognosis";
import type { ModelInfo } from "@/types/api";

const model = {
  input_price_per_m: "100",
  output_price_per_m: "300",
} as unknown as ModelInfo;

describe("estimatePrognosis", () => {
  it("counts text at ~4 chars/token", () => {
    // 8 chars → ceil(8/4) = 2 tokens, no history/images/files.
    const r = estimatePrognosis({
      historyTokens: 0,
      text: "abcdefgh",
      images: 0,
      files: [],
      model,
    });
    expect(r.inputTokens).toBe(2);
  });

  it("adds history, 1000 tokens per image, and file token estimates", () => {
    const r = estimatePrognosis({
      historyTokens: 500,
      text: "abcd", // 1 token
      images: 2, // 2000 tokens
      files: [{ tokenEstimate: 120 }, { tokenEstimate: 30 }], // 150 tokens
      model,
    });
    expect(r.inputTokens).toBe(500 + 1 + 2000 + 150);
  });

  it("prices input tokens + a fixed 500-token output estimate", () => {
    const r = estimatePrognosis({
      historyTokens: 0,
      text: "", // 0 tokens
      images: 1, // 1000 input tokens
      files: [],
      model,
    });
    // input: 1000/1e6 * 100 = 0.1 ; output: 500/1e6 * 300 = 0.15
    expect(r.inputTokens).toBe(1000);
    expect(r.costRub).toBeCloseTo(0.25, 10);
  });

  it("treats a missing model as zero price (never NaN)", () => {
    const r = estimatePrognosis({
      historyTokens: 10,
      text: "abcd",
      images: 0,
      files: [],
      model: undefined,
    });
    expect(r.inputTokens).toBe(11);
    expect(r.costRub).toBe(0);
  });
});
