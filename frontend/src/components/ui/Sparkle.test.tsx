import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Sparkle } from "@/components/ui/Sparkle";

describe("Sparkle", () => {
  it("renders an svg sized from the size prop", () => {
    const { container } = render(<Sparkle size={40} />);
    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(svg).toHaveAttribute("width", "40");
    expect(svg).toHaveAttribute("height", "40");
  });

  it("renders the multi-ray burst (12 rays + a centre dot)", () => {
    const { container } = render(<Sparkle />);
    expect(container.querySelectorAll("line")).toHaveLength(12);
    expect(container.querySelectorAll("circle")).toHaveLength(1);
  });
});
