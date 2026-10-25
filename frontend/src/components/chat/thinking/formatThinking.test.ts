import { describe, it, expect } from "vitest";
import {
  formatDurationSec,
  formatReasoningTokens,
  buildHeaderMeta,
} from "./formatThinking";

describe("formatDurationSec", () => {
  it("rounds ms to whole seconds with a Russian suffix", () => {
    expect(formatDurationSec(6000)).toBe("6с");
    expect(formatDurationSec(6400)).toBe("6с");
    expect(formatDurationSec(6600)).toBe("7с");
  });
  it("never goes negative", () => {
    expect(formatDurationSec(-50)).toBe("0с");
  });
});

describe("formatReasoningTokens", () => {
  it("shows raw count below 1000", () => {
    expect(formatReasoningTokens(800)).toBe("800 токенов");
  });
  it("compacts thousands", () => {
    expect(formatReasoningTokens(1200)).toBe("1.2k токенов");
    expect(formatReasoningTokens(12000)).toBe("12k токенов");
  });
  it("strips a trailing .0", () => {
    expect(formatReasoningTokens(1000)).toBe("1k токенов");
    expect(formatReasoningTokens(10000)).toBe("10k токенов");
  });
  it("uses Russian plural forms below 1000 (tr-cy0.3.13)", () => {
    expect(formatReasoningTokens(1)).toBe("1 токен");
    expect(formatReasoningTokens(2)).toBe("2 токена");
    expect(formatReasoningTokens(5)).toBe("5 токенов");
    expect(formatReasoningTokens(21)).toBe("21 токен");
    expect(formatReasoningTokens(24)).toBe("24 токена");
    expect(formatReasoningTokens(11)).toBe("11 токенов"); // teens are "many"
    expect(formatReasoningTokens(109)).toBe("109 токенов");
  });
  it("never produces the «планов» artifact in any form", () => {
    for (const n of [1, 2, 5, 11, 21, 24, 109, 1200, 12000]) {
      expect(formatReasoningTokens(n)).not.toMatch(/план/);
    }
  });
});

describe("buildHeaderMeta", () => {
  it("joins duration and tokens with a middot", () => {
    expect(buildHeaderMeta({ durationMs: 6000, reasoningTokens: 1200 })).toBe(
      "6с · 1.2k токенов",
    );
  });
  it("omits missing parts", () => {
    expect(buildHeaderMeta({ reasoningTokens: 800 })).toBe("800 токенов");
    expect(buildHeaderMeta({ durationMs: 3000 })).toBe("3с");
    expect(buildHeaderMeta({})).toBe("");
  });
  it("omits zero tokens", () => {
    expect(buildHeaderMeta({ durationMs: 3000, reasoningTokens: 0 })).toBe("3с");
  });
});
