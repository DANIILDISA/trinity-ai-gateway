import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";

import { SectionShell } from "./primitives";

describe("SectionShell — header (tr-a7fg)", () => {
  it("renders the section title once, with no duplicate '// title' eyebrow", () => {
    render(
      <SectionShell title="Конфиденциальность">
        <div>тело</div>
      </SectionShell>,
    );
    // The title shows as the section heading…
    expect(
      screen.getByRole("heading", { name: "Конфиденциальность" }),
    ).toBeInTheDocument();
    // …and NOT also as a decorative "// конфиденциальность" eyebrow above it.
    expect(screen.queryByText(/^\/\//)).toBeNull();
  });
});
