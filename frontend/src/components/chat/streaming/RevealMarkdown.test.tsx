import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RevealMarkdown } from "./RevealMarkdown";

beforeEach(() => {
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    cb(0);
    return 1;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
  vi.stubGlobal("matchMedia", (q: string) => ({
    matches: false,
    media: q,
    addEventListener() {},
    removeEventListener() {},
  }));
});
afterEach(() => vi.unstubAllGlobals());

describe("RevealMarkdown", () => {
  it("renders the full content instantly when not streaming", () => {
    render(<RevealMarkdown content="Привет мир" streaming={false} />);
    expect(screen.getByText(/Привет мир/)).toBeInTheDocument();
  });
});
