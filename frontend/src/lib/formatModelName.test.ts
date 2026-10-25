import { describe, it, expect } from "vitest";

import { formatModelName } from "./formatting";

// The per-version meta line (MessageBubble) and the ChatSidebar recent list show
// a friendly model label instead of the raw id. These tests pin that mapping:
// the provider prefix is dropped, hyphens become spaces, and each word's first
// letter is upper-cased (matching the prior ChatSidebar.shortModelName behavior).
describe("formatModelName", () => {
  it("strips the provider prefix and title-cases the name", () => {
    expect(formatModelName("anthropic/claude-opus-4.5")).toBe("Claude Opus 4.5");
  });

  it("upper-cases each word's first letter for non-vendor ids", () => {
    expect(formatModelName("openai/gpt-oss-20b")).toBe("Gpt Oss 20b");
  });

  it("handles an id without a provider prefix", () => {
    expect(formatModelName("gemini-2.5-pro")).toBe("Gemini 2.5 Pro");
  });

  it("keeps version-suffix tokens intact", () => {
    expect(formatModelName("deepseek/deepseek-v3")).toBe("Deepseek V3");
  });
});
