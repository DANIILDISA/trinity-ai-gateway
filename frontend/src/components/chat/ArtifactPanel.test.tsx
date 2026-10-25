import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("@/lib/highlight", () => ({
  highlightToHtml: vi.fn().mockResolvedValue(null),
}));

import type { Artifact } from "@/lib/artifacts";
import { ArtifactPanel } from "./ArtifactPanel";

const art: Artifact = {
  id: "markdown-x",
  kind: "markdown",
  title: "Python exam prep",
  content: "# Заголовок документа\n\nабзац текста",
  source: "heuristic",
};

const base = {
  view: "preview" as const,
  onChangeView: () => {},
  onClose: () => {},
};

beforeEach(() => {
  Object.assign(navigator, {
    clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
});

describe("ArtifactPanel", () => {
  it("renders the title with a kind suffix", () => {
    render(<ArtifactPanel artifact={art} {...base} />);
    expect(screen.getByText("Python exam prep")).toBeInTheDocument();
    expect(screen.getByText("· MD")).toBeInTheDocument();
  });

  it("switches view through the segmented toggle", async () => {
    const onChangeView = vi.fn();
    render(<ArtifactPanel artifact={art} {...base} onChangeView={onChangeView} />);
    await userEvent.click(screen.getByRole("button", { name: "Код" }));
    expect(onChangeView).toHaveBeenCalledWith("code");
  });

  it("renders rendered markdown in preview view", () => {
    const { container } = render(<ArtifactPanel artifact={art} {...base} view="preview" />);
    expect(container.querySelector("h1")).not.toBeNull();
    expect(screen.getByText("Заголовок документа")).toBeInTheDocument();
  });

  it("renders the raw source (with markdown syntax) in code view", () => {
    render(<ArtifactPanel artifact={art} {...base} view="code" />);
    expect(screen.getByText(/# Заголовок документа/)).toBeInTheDocument();
  });

  it("copies the current content and shows Скопировано", async () => {
    render(<ArtifactPanel artifact={art} {...base} />);
    await userEvent.click(screen.getByRole("button", { name: "Скопировать" }));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(art.content);
    expect(await screen.findByText("Скопировано")).toBeInTheDocument();
  });

  it("offers markdown and plain-text copy options in the dropdown", async () => {
    render(<ArtifactPanel artifact={art} {...base} />);
    await userEvent.click(screen.getByRole("button", { name: "Параметры копирования" }));
    expect(screen.getByText("Копировать как Markdown")).toBeInTheDocument();
    expect(screen.getByText("Копировать как обычный текст")).toBeInTheDocument();
  });

  it("shows the regenerate action only when onRegenerate is provided", () => {
    const { rerender } = render(<ArtifactPanel artifact={art} {...base} />);
    expect(screen.queryByRole("button", { name: "Перегенерировать" })).toBeNull();
    rerender(<ArtifactPanel artifact={art} {...base} onRegenerate={() => {}} />);
    expect(screen.getByRole("button", { name: "Перегенерировать" })).toBeInTheDocument();
  });

  it("hides the version badge for heuristic artifacts even with version>1", () => {
    render(<ArtifactPanel artifact={{ ...art, version: 3, source: "heuristic" }} {...base} />);
    expect(screen.queryByText("v3")).toBeNull();
  });

  it("shows the version badge for server artifacts with version>1", () => {
    render(<ArtifactPanel artifact={{ ...art, version: 3, source: "server" }} {...base} />);
    expect(screen.getByText("v3")).toBeInTheDocument();
  });

  it("shows no version switcher for a single-version artifact", () => {
    render(
      <ArtifactPanel
        artifact={{ ...art, source: "server", version: 1, versionCount: 1 }}
        {...base}
        onSwitchVersion={() => {}}
      />,
    );
    expect(screen.queryByLabelText("Версия")).toBeNull();
    expect(screen.queryByRole("button", { name: "Предыдущая версия" })).toBeNull();
  });

  it("shows an n/m switcher for a multi-version artifact", () => {
    render(
      <ArtifactPanel
        artifact={{ ...art, source: "server", version: 2, versionCount: 2 }}
        {...base}
        onSwitchVersion={() => {}}
      />,
    );
    expect(screen.getByText("2/2")).toBeInTheDocument();
  });

  it("requests the previous version when ‹ is clicked", async () => {
    const onSwitchVersion = vi.fn();
    render(
      <ArtifactPanel
        artifact={{ ...art, source: "server", version: 2, versionCount: 2 }}
        {...base}
        onSwitchVersion={onSwitchVersion}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Предыдущая версия" }));
    expect(onSwitchVersion).toHaveBeenCalledWith(1);
  });

  it("requests the next version when › is clicked", async () => {
    const onSwitchVersion = vi.fn();
    render(
      <ArtifactPanel
        artifact={{ ...art, source: "server", version: 1, versionCount: 2 }}
        {...base}
        onSwitchVersion={onSwitchVersion}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Следующая версия" }));
    expect(onSwitchVersion).toHaveBeenCalledWith(2);
  });

  it("renders a code artifact as source even in preview view — never through Markdown (tr-893i)", () => {
    const codeArt: Artifact = {
      id: "code-x",
      kind: "code",
      language: "python",
      title: "script.py",
      content: "# this is a comment\nprint('hi')",
      source: "server",
    };
    const { container } = render(
      <ArtifactPanel artifact={codeArt} {...base} view="preview" />,
    );
    // The Python comment must NOT be rendered as a Markdown <h1>.
    expect(container.querySelector("h1")).toBeNull();
    // The raw source (with the leading # comment) is shown instead.
    expect(screen.getByText(/# this is a comment/)).toBeInTheDocument();
  });

  it("hides the Preview/Code toggle for a code artifact (no meaningful preview) (tr-893i)", () => {
    const codeArt: Artifact = {
      id: "code-y",
      kind: "code",
      language: "python",
      title: "script.py",
      content: "print('hi')",
      source: "server",
    };
    render(<ArtifactPanel artifact={codeArt} {...base} view="preview" />);
    expect(screen.queryByRole("button", { name: "Превью" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Код" })).toBeNull();
  });

  it("closes via the X button and via Escape", async () => {
    const onClose = vi.fn();
    render(<ArtifactPanel artifact={art} {...base} onClose={onClose} />);
    await userEvent.click(screen.getByRole("button", { name: "Закрыть" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});

describe("ArtifactPanel accessibility", () => {
  it("is a labelled region in split mode", () => {
    render(<ArtifactPanel artifact={art} {...base} />);
    expect(
      screen.getByRole("region", { name: "Документ Python exam prep" }),
    ).toBeInTheDocument();
  });

  it("is a modal dialog in fullscreen mode", () => {
    render(<ArtifactPanel artifact={art} {...base} fullscreen />);
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
  });

  it("moves focus to the Превью toggle on open", () => {
    render(<ArtifactPanel artifact={art} {...base} />);
    expect(screen.getByRole("button", { name: "Превью" })).toHaveFocus();
  });

  it("locks body scroll only in fullscreen mode", () => {
    const { unmount } = render(<ArtifactPanel artifact={art} {...base} fullscreen />);
    expect(document.body.style.overflow).toBe("hidden");
    unmount();
    expect(document.body.style.overflow).toBe("");
  });

  it("does not lock body scroll in split mode", () => {
    render(<ArtifactPanel artifact={art} {...base} />);
    expect(document.body.style.overflow).toBe("");
  });

  it("Escape closes an open copy menu before closing the panel", async () => {
    const onClose = vi.fn();
    render(<ArtifactPanel artifact={art} {...base} onClose={onClose} />);
    await userEvent.click(screen.getByRole("button", { name: "Параметры копирования" }));
    expect(screen.getByText("Копировать как Markdown")).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");
    expect(screen.queryByText("Копировать как Markdown")).toBeNull();
    expect(onClose).not.toHaveBeenCalled();

    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
