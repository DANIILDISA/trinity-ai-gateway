import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { ReasoningSegment } from "./ReasoningSegment";

describe("ReasoningSegment — titled self-collapse (tr-cy0.3.10)", () => {
  it("renders the summary as a collapsed header, body hidden by default", () => {
    render(
      <ReasoningSegment
        segment={{
          kind: "reasoning",
          id: "seg-1",
          text: "тело размышления",
          summary: "Анализирую вопросы",
        }}
      />,
    );
    const btn = screen.getByRole("button", { name: /Анализирую вопросы/ });
    expect(btn).toHaveAttribute("aria-expanded", "false");
  });

  it("toggles aria-expanded on click", () => {
    render(
      <ReasoningSegment
        segment={{ kind: "reasoning", id: "seg-1", text: "x", summary: "Заголовок" }}
      />,
    );
    const btn = screen.getByRole("button");
    fireEvent.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "false");
  });

  it("aria-controls references the body element", () => {
    render(
      <ReasoningSegment
        segment={{ kind: "reasoning", id: "seg-1", text: "тело", summary: "Заголовок" }}
      />,
    );
    const id = screen.getByRole("button").getAttribute("aria-controls");
    expect(id).toBeTruthy();
    expect(document.getElementById(id!)).not.toBeNull();
  });

  it("shows a pulse skeleton (aria-busy) while the summary is pending", () => {
    render(
      <ReasoningSegment
        segment={{ kind: "reasoning", id: "seg-1", text: "x", summaryPending: true }}
      />,
    );
    const skeleton = screen.getByLabelText("Готовится заголовок");
    expect(skeleton).toBeInTheDocument();
    expect(skeleton).toHaveAttribute("aria-busy", "true");
  });

  it("titles an untitled segment from its text (fallback), no skeleton (tr-cy0.3.14)", () => {
    // A reloaded / post-Stop segment with no persisted summary and no pending
    // generation: the header shows a text-derived title, not a stuck skeleton.
    render(
      <ReasoningSegment
        segment={{
          kind: "reasoning",
          id: "seg-0",
          text: "Первая значимая строка\nостальное тело",
        }}
      />,
    );
    expect(
      screen.getByRole("button", { name: /Первая значимая строка/ }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Готовится заголовок")).toBeNull();
  });

  // tr-cy0.3.19: a fenced code block in the reasoning text renders as a
  // highlighted code panel (like the main answer), not a default prose <pre>.
  it("renders fenced code as a highlighted code panel", () => {
    const { container } = render(
      <ReasoningSegment
        segment={{
          kind: "reasoning",
          id: "seg-0",
          text: "Смотрю код:\n\n```python\ndef f():\n    return 1\n```",
          summary: "Смотрю код",
        }}
      />,
    );
    // The dark CodeBlock panel (same surface as the main answer) is present even
    // while collapsed (body stays mounted in the grid-rows transition).
    expect(container.querySelector('[class*="161615"]')).not.toBeNull();
  });
});
