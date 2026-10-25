import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";

import { Markdown } from "./Markdown";

describe("Markdown", () => {
  it("renders a GFM table with header and body cells", () => {
    const table = ["| A | B |", "| - | - |", "| 1 | 2 |"].join("\n");
    const { container } = render(<Markdown content={table} />);

    const tableEl = container.querySelector("table");
    expect(tableEl).not.toBeNull();
    expect(container.querySelector("th")).not.toBeNull();
    expect(container.querySelector("td")).not.toBeNull();
  });

  it("renders inline LaTeX math via KaTeX", () => {
    const { container } = render(<Markdown content="$x^2$" />);
    expect(container.querySelector(".katex")).not.toBeNull();
  });

  it("renders literal dollar amounts as currency, not math (tr-ymda)", () => {
    const { container } = render(
      <Markdown content="It costs $5 today and $10 tomorrow." />
    );
    // The $ are currency delimiters of nothing — must NOT become KaTeX math.
    expect(container.querySelector(".katex")).toBeNull();
    // Both amounts render literally, dollar signs intact.
    expect(container.textContent).toContain("$5");
    expect(container.textContent).toContain("$10");
    expect(container.textContent).toContain(
      "It costs $5 today and $10 tomorrow."
    );
  });

  it("still renders single-$ LaTeX that does not start with a digit (tr-ymda)", () => {
    // Currency-escaping must not regress real inline math like $x^2$ or $S_{..}$.
    const { container } = render(
      <Markdown content="Currency $5 and math $x^2$ together." />
    );
    expect(container.querySelector(".katex")).not.toBeNull();
    expect(container.textContent).toContain("$5");
  });

  it("does not inject a backslash into dollars inside code spans (tr-ymda)", () => {
    const { container } = render(<Markdown content={"Run `echo $5` now"} />);
    const code = container.querySelector("code");
    expect(code?.textContent).toBe("echo $5");
  });

  it("does not flood console with KaTeX strict warnings for Cyrillic in math mode", () => {
    // tr-g1j: RU market — physics/math with Russian subscripts/labels. The model
    // writes e.g. $S_{между}$; KaTeX strict='warn' (default) logs one warning per
    // Cyrillic char (197 on a single answer live).
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      const { container } = render(
        <Markdown content={"Расстояние $S_{между} = 650 - 40 = 610$ км"} />
      );
      // Still renders, and the Cyrillic subscript stays readable (not dropped).
      expect(container.querySelector(".katex")).not.toBeNull();
      expect(container.textContent).toContain("между");
      const messages = warn.mock.calls.map((c) => c.join(" "));
      expect(messages.some((m) => m.includes("unicodeTextInMathMode"))).toBe(false);
    } finally {
      warn.mockRestore();
    }
  });
});
