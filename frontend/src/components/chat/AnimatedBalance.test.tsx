import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { AnimatedBalance } from "./AnimatedBalance";

// Force reduced motion so the value snaps deterministically (the rAF count-up
// itself is timing-driven; these tests assert the rendered value + formatting).
beforeEach(() => {
  vi.stubGlobal("matchMedia", (q: string) => ({
    matches: true,
    media: q,
    addEventListener() {},
    removeEventListener() {},
  }));
});
afterEach(() => vi.unstubAllGlobals());

describe("AnimatedBalance (tr-hye6)", () => {
  it("renders the formatted balance", () => {
    render(<AnimatedBalance apiString="100.00" />);
    expect(screen.getByText(/100\.00/)).toBeInTheDocument();
  });

  it("updates to the new value when the balance changes", () => {
    const { rerender } = render(<AnimatedBalance apiString="100.00" />);
    expect(screen.getByText(/100\.00/)).toBeInTheDocument();
    rerender(<AnimatedBalance apiString="600.00" />);
    expect(screen.getByText(/600\.00/)).toBeInTheDocument();
    expect(screen.queryByText(/100\.00/)).toBeNull();
  });
});
