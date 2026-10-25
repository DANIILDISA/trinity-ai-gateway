import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Tooltip } from "@/components/ui/Tooltip";

describe("Tooltip", () => {
  it("renders the trigger children and a role=tooltip carrying the label", () => {
    render(
      <Tooltip label="Стоимость ответа: 1.00 ₽">
        <span>icon</span>
      </Tooltip>,
    );
    expect(screen.getByText("icon")).toBeInTheDocument();
    const tip = screen.getByRole("tooltip");
    expect(tip).toHaveTextContent("Стоимость ответа: 1.00 ₽");
  });

  it("places the bubble above the trigger by default (side=top)", () => {
    render(
      <Tooltip label="hint">
        <span>x</span>
      </Tooltip>,
    );
    // top placement anchors the bubble to the trigger's bottom edge.
    expect(screen.getByRole("tooltip").className).toContain("bottom-full");
  });

  it("supports bottom placement", () => {
    render(
      <Tooltip label="hint" side="bottom">
        <span>x</span>
      </Tooltip>,
    );
    expect(screen.getByRole("tooltip").className).toContain("top-full");
  });
});
