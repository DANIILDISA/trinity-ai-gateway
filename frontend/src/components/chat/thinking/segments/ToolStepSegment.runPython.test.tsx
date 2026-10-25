import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ToolStepSegment } from "./ToolStepSegment";

describe("ToolStepSegment — run_python run-card (tr-7so1)", () => {
  it("shows the submitted code, its output and the run duration", () => {
    const { container } = render(
      <ToolStepSegment
        segment={{
          kind: "tool_call",
          id: "tc-0-0",
          tool: "run_python",
          title: "Выполнение Python",
          status: "done",
          input: JSON.stringify({ code: "print(2 + 2)" }),
          output: "4",
          duration_ms: 1234,
        }}
      />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain("Выполнение Python");
    expect(text).toContain("print(2 + 2)");
    expect(text).toContain("4");
    expect(text).toMatch(/выполнено за/i);
  });

  it("marks an error run and shows the truncation marker", () => {
    const { container } = render(
      <ToolStepSegment
        segment={{
          kind: "tool_call",
          id: "tc-0-1",
          tool: "run_python",
          title: "Выполнение Python",
          status: "error",
          input: JSON.stringify({ code: "boom" }),
          output: "Traceback (most recent call last)\nNameError: name 'boom' is not defined",
          duration_ms: 50,
          truncated: true,
        }}
      />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain("Traceback");
    expect(text).toMatch(/обрез/i);
  });
});
