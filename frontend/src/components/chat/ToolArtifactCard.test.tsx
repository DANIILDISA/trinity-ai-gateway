import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ToolArtifactCard } from "./ToolArtifactCard";
import {
  downloadToolArtifact,
  fetchToolArtifact,
} from "@/lib/downloadToolArtifact";

vi.mock("@/lib/downloadToolArtifact", () => ({
  downloadToolArtifact: vi.fn().mockResolvedValue(undefined),
  fetchToolArtifact: vi.fn(),
}));

const artifact = {
  filename: "python_exam_prep.md",
  title: "Python exam prep",
  typeLabel: "Документ · MD",
};

describe("ToolArtifactCard", () => {
  beforeEach(() => {
    vi.mocked(downloadToolArtifact).mockReset().mockResolvedValue(undefined);
    vi.mocked(fetchToolArtifact).mockReset().mockResolvedValue({
      filename: "python_exam_prep.md",
      media_type: "text/markdown",
      content: "# Python exam prep\n\nТекст.",
      version: 1,
      version_count: 1,
    });
  });

  it("renders the title, the type label and a download button", () => {
    render(<ToolArtifactCard artifact={artifact} conversationId="conv-1" />);
    expect(screen.getByText("Python exam prep")).toBeInTheDocument();
    expect(screen.getByText("Документ · MD")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Скачать/ }),
    ).toBeInTheDocument();
  });

  // tr-cy0.5.12 / tr-cy0.6.13: compact (content-width) card with a tight margin
  // so it doesn't span the feed and stacked cards sit almost flush.
  it("is compact (shrink-to-content) with a tight vertical margin", () => {
    const { container } = render(
      <ToolArtifactCard artifact={artifact} conversationId="conv-1" />,
    );
    const root = container.firstChild as HTMLElement;
    expect(root.className).toContain("w-fit");
    expect(root.className).not.toContain("w-full");
    // No additive margin — the message column's flex gap-1 separates stacked
    // cards so they sit almost flush.
    expect(root.className).toContain("my-0");
    expect(root.className).not.toContain("my-3");
    expect(root.className).not.toContain("my-1.5");
  });

  it("downloads via the endpoint when clicked", async () => {
    render(<ToolArtifactCard artifact={artifact} conversationId="conv-1" />);
    await userEvent.click(screen.getByRole("button", { name: /Скачать/ }));
    await waitFor(() =>
      expect(downloadToolArtifact).toHaveBeenCalledWith(
        "conv-1",
        "python_exam_prep.md",
      ),
    );
  });

  it("disables download when there is no conversation id", () => {
    render(<ToolArtifactCard artifact={artifact} />);
    expect(screen.getByRole("button", { name: /Скачать/ })).toBeDisabled();
  });

  it("opens the canvas with fetched content on body click (tr-lqn)", async () => {
    const onOpenArtifact = vi.fn();
    render(
      <ToolArtifactCard
        artifact={artifact}
        conversationId="conv-1"
        onOpenArtifact={onOpenArtifact}
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Открыть документ: Python exam prep/ }),
    );
    await waitFor(() => expect(onOpenArtifact).toHaveBeenCalledTimes(1));
    expect(fetchToolArtifact).toHaveBeenCalledWith("conv-1", "python_exam_prep.md");
    expect(onOpenArtifact.mock.calls[0][0]).toMatchObject({
      kind: "markdown",
      title: "Python exam prep",
      content: "# Python exam prep\n\nТекст.",
      source: "server",
    });
  });

  it("disables the open body when no onOpenArtifact handler is provided", () => {
    render(<ToolArtifactCard artifact={artifact} conversationId="conv-1" />);
    expect(
      screen.getByRole("button", { name: /Открыть документ/ }),
    ).toBeDisabled();
  });

  it("surfaces a failed-open subtitle without triggering a download (tr-lqn)", async () => {
    vi.mocked(fetchToolArtifact).mockRejectedValueOnce(new Error("boom"));
    const onOpenArtifact = vi.fn();
    render(
      <ToolArtifactCard
        artifact={artifact}
        conversationId="conv-1"
        onOpenArtifact={onOpenArtifact}
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Открыть документ/ }),
    );
    await waitFor(() =>
      expect(screen.getByText("Не удалось открыть")).toBeInTheDocument(),
    );
    expect(onOpenArtifact).not.toHaveBeenCalled();
    expect(downloadToolArtifact).not.toHaveBeenCalled();
  });

  it("keeps a long title recoverable via a native title tooltip when truncated", () => {
    const longTitle = "Очень длинный заголовок документа подготовки к экзамену";
    render(
      <ToolArtifactCard
        artifact={{ ...artifact, title: longTitle }}
        conversationId="conv-1"
      />,
    );
    const titleEl = screen.getByText(longTitle);
    expect(titleEl).toHaveAttribute("title", longTitle);
    expect(titleEl.className).toContain("truncate");
  });
});
