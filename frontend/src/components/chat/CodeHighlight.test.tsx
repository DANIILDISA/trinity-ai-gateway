import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

vi.mock("@/lib/highlight", () => ({
  highlightToHtml: vi.fn(),
}));
import { highlightToHtml } from "@/lib/highlight";
import { CodeHighlight } from "./CodeHighlight";

const mockHighlight = vi.mocked(highlightToHtml);

describe("CodeHighlight", () => {
  beforeEach(() => mockHighlight.mockReset());

  it("shows the raw code as a fallback first, then swaps to highlighted HTML", async () => {
    mockHighlight.mockResolvedValue(
      '<pre class="shiki"><code><span style="color:#c678dd">def</span></code></pre>',
    );
    const { container } = render(<CodeHighlight code={"def f(): pass"} language="python" />);
    // First synchronous paint is the plain fallback (no layout shift, SSR-safe).
    expect(screen.getByText("def f(): pass")).toBeInTheDocument();
    expect(container.querySelector("pre.shiki")).toBeNull();
    // Once the highlighter resolves, the colored version replaces it.
    await waitFor(() => expect(container.querySelector("pre.shiki")).not.toBeNull());
  });

  it("keeps the plain fallback when highlighting is unavailable (null)", async () => {
    mockHighlight.mockResolvedValue(null);
    render(<CodeHighlight code={"SELECT 1"} language="cobol" />);
    await waitFor(() => expect(mockHighlight).toHaveBeenCalled());
    expect(screen.getByText("SELECT 1")).toBeInTheDocument();
  });
});
