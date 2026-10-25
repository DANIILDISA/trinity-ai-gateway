import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { Artifact } from "@/lib/artifacts";
import { ArtifactCard } from "./ArtifactCard";

const mdArtifact: Artifact = {
  id: "markdown-x",
  kind: "markdown",
  title: "Python exam prep",
  content: "# Подготовка",
  source: "heuristic",
};

const codeArtifact: Artifact = {
  id: "code-y",
  kind: "code",
  language: "python",
  title: "script",
  content: "print(1)",
  source: "heuristic",
};

const noop = () => {};

describe("ArtifactCard", () => {
  it("shows the title and the markdown label with a middle-dot separator", () => {
    render(<ArtifactCard artifact={mdArtifact} onOpen={noop} onDownload={noop} />);
    expect(screen.getByText("Python exam prep")).toBeInTheDocument();
    expect(screen.getByText("Документ · MD")).toBeInTheDocument();
  });

  it("reflects the code language in the label", () => {
    render(<ArtifactCard artifact={codeArtifact} onOpen={noop} onDownload={noop} />);
    expect(screen.getByText("Код · PYTHON")).toBeInTheDocument();
  });

  it("opens the canvas when the card body is clicked", async () => {
    const onOpen = vi.fn();
    const onDownload = vi.fn();
    render(<ArtifactCard artifact={mdArtifact} onOpen={onOpen} onDownload={onDownload} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Открыть документ: Python exam prep" }),
    );
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onDownload).not.toHaveBeenCalled();
  });

  it("downloads without opening the panel", async () => {
    const onOpen = vi.fn();
    const onDownload = vi.fn();
    render(<ArtifactCard artifact={mdArtifact} onOpen={onOpen} onDownload={onDownload} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Скачать Python exam prep" }),
    );
    expect(onDownload).toHaveBeenCalledTimes(1);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("exposes accessible names for both the card and the download action", () => {
    render(<ArtifactCard artifact={mdArtifact} onOpen={noop} onDownload={noop} />);
    expect(
      screen.getByRole("button", { name: "Открыть документ: Python exam prep" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Скачать Python exam prep" }),
    ).toBeInTheDocument();
  });

  it("shows a generating state with a disabled download while streaming", () => {
    render(
      <ArtifactCard artifact={mdArtifact} onOpen={noop} onDownload={noop} generating />,
    );
    expect(screen.getByText("Генерация…")).toBeInTheDocument();
    expect(screen.queryByText("Документ · MD")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Скачать Python exam prep" }),
    ).toBeDisabled();
  });

  it("opens via keyboard (Enter) when the card body is focused", async () => {
    const onOpen = vi.fn();
    render(<ArtifactCard artifact={mdArtifact} onOpen={onOpen} onDownload={noop} />);
    screen.getByRole("button", { name: "Открыть документ: Python exam prep" }).focus();
    await userEvent.keyboard("{Enter}");
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  // tr-cy0.5.12: the card must size to its content (name + Скачать), not stretch
  // to the full feed width. tr-cy0.6.13: a compact vertical margin so stacked
  // cards sit almost flush.
  it("is compact (shrink-to-content) with a tight vertical margin", () => {
    const { container } = render(
      <ArtifactCard artifact={mdArtifact} onOpen={noop} onDownload={noop} />,
    );
    const root = container.firstChild as HTMLElement;
    expect(root.className).toContain("w-fit");
    expect(root.className).not.toContain("w-full");
    expect(root.className).toContain("my-1.5");
    expect(root.className).not.toContain("my-3");
  });
});
