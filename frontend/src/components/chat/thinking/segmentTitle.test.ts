import { describe, it, expect } from "vitest";

import { segmentTitle } from "./segmentTitle";

describe("segmentTitle (tr-cy0.3.10)", () => {
  it("uses the summary when present", () => {
    expect(
      segmentTitle({ summary: "Анализирую вопросы", text: "тело" }),
    ).toBe("Анализирую вопросы");
  });

  it("falls back to the first significant line of text", () => {
    expect(
      segmentTitle({ text: "  \n\nПервая значимая строка\nвторая" }),
    ).toBe("Первая значимая строка");
  });

  it("strips markdown heading / bold / list lead from the fallback line", () => {
    expect(segmentTitle({ text: "## Заголовок раздела\nтело" })).toBe(
      "Заголовок раздела",
    );
    expect(segmentTitle({ text: "**Жирный лид** дальше" })).toBe(
      "Жирный лид дальше",
    );
    expect(segmentTitle({ text: "- пункт списка" })).toBe("пункт списка");
  });

  it("prefers a non-empty summary over text", () => {
    expect(segmentTitle({ summary: "  ", text: "fallback line" })).toBe(
      "fallback line",
    );
  });
});
