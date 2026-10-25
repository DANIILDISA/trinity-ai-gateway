import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { ThinkingBody } from "./ThinkingBody";

// jsdom reports scrollHeight as 0 (no layout); override the getter so the
// overflow measurement can be exercised. vi.restoreAllMocks() puts it back.
function mockScrollHeight(px: number) {
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(px);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ThinkingBody", () => {
  it("renders its children", () => {
    render(
      <ThinkingBody active={false}>
        <p>reasoning text</p>
      </ThinkingBody>,
    );
    expect(screen.getByText("reasoning text")).toBeInTheDocument();
  });

  it("shows no toggle when content fits", () => {
    mockScrollHeight(40);
    render(
      <ThinkingBody active={false}>
        <p>short</p>
      </ThinkingBody>,
    );
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("offers 'Показать больше' when content overflows and toggles on click", () => {
    mockScrollHeight(1000);
    render(
      <ThinkingBody active={false}>
        <p>long reasoning</p>
      </ThinkingBody>,
    );
    const more = screen.getByRole("button", { name: "Показать больше" });
    expect(more).toBeInTheDocument();
    fireEvent.click(more);
    expect(
      screen.getByRole("button", { name: "Показать меньше" }),
    ).toBeInTheDocument();
    // content stays in the DOM throughout
    expect(screen.getByText("long reasoning")).toBeInTheDocument();
  });

  it("never clamps or shows a toggle while active (streaming)", () => {
    mockScrollHeight(1000);
    render(
      <ThinkingBody active={true}>
        <p>streaming</p>
      </ThinkingBody>,
    );
    expect(screen.queryByRole("button")).toBeNull();
  });
});
