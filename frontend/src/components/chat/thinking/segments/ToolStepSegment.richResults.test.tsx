import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// ImageResult reads the conversation id from the route to re-fetch a persisted plot;
// stub it so the live (base64-present) path renders without a Next router.
vi.mock("next/navigation", () => ({ useParams: () => ({ id: "conv-1" }) }));

import { ToolStepSegment } from "./ToolStepSegment";

describe("ToolStepSegment — rich outputs (tr-5ktl)", () => {
  it("renders a matplotlib PNG inline as a data-URI image", () => {
    const { container } = render(
      <ToolStepSegment
        segment={{
          kind: "tool_call",
          id: "tc-0-0",
          tool: "run_python",
          title: "Выполнение Python",
          status: "done",
          input: JSON.stringify({ code: "plt.plot([1,2]); plt.show()" }),
          results: [{ type: "image/png", data: "AAABBB" }],
        }}
      />,
    );
    const img = container.querySelector("img");
    expect(img?.getAttribute("src")).toBe("data:image/png;base64,AAABBB");
  });

  it("renders a DataFrame HTML as a text table, stripping any markup", () => {
    const html =
      "<table><thead><tr><th>col</th></tr></thead><tbody>" +
      "<tr><td>1</td></tr><tr><td><script>alert(1)</script>x</td></tr></tbody></table>";
    const { container } = render(
      <ToolStepSegment
        segment={{
          kind: "tool_call",
          id: "tc-0-1",
          tool: "run_python",
          title: "Выполнение Python",
          status: "done",
          results: [{ type: "text/html", data: html }],
        }}
      />,
    );
    expect(container.querySelector("table")).not.toBeNull();
    expect(container.querySelector("script")).toBeNull(); // injected markup never lands
    const text = container.textContent ?? "";
    expect(text).toContain("col");
    expect(text).toContain("1");
  });

  it("renders a text/plain result verbatim", () => {
    const { container } = render(
      <ToolStepSegment
        segment={{
          kind: "tool_call",
          id: "tc-0-2",
          tool: "run_python",
          title: "Выполнение Python",
          status: "done",
          results: [{ type: "text/plain", data: "42" }],
        }}
      />,
    );
    expect(container.textContent ?? "").toContain("42");
  });
});
