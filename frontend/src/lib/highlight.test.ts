import { describe, it, expect } from "vitest";

import { normalizeLang } from "./highlight";

describe("normalizeLang", () => {
  it("passes through a supported canonical language", () => {
    expect(normalizeLang("python")).toBe("python");
    expect(normalizeLang("typescript")).toBe("typescript");
  });

  it("maps common aliases to their canonical language", () => {
    expect(normalizeLang("py")).toBe("python");
    expect(normalizeLang("sh")).toBe("bash");
    expect(normalizeLang("shell")).toBe("bash");
    expect(normalizeLang("yml")).toBe("yaml");
    expect(normalizeLang("ts")).toBe("typescript");
    expect(normalizeLang("js")).toBe("javascript");
  });

  it("is case-insensitive and trims surrounding whitespace", () => {
    expect(normalizeLang("  TS  ")).toBe("typescript");
    expect(normalizeLang("Python")).toBe("python");
  });

  it("falls back to 'text' for missing or empty input", () => {
    expect(normalizeLang(undefined)).toBe("text");
    expect(normalizeLang("")).toBe("text");
    expect(normalizeLang("   ")).toBe("text");
  });

  it("falls back to 'text' for an unsupported language", () => {
    expect(normalizeLang("cobol")).toBe("text");
    expect(normalizeLang("language-foo")).toBe("text");
  });
});
