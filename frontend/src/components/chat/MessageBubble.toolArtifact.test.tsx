import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("@/lib/highlight", () => ({
  highlightToHtml: vi.fn().mockResolvedValue(null),
}));
const downloadToolArtifact = vi.fn().mockResolvedValue(undefined);
const fetchToolArtifact = vi.fn().mockResolvedValue({
  filename: "python_exam_prep.md",
  media_type: "text/markdown",
  content: "# Python exam prep\n\nСодержимое документа.",
  version: 1,
  version_count: 1,
});
vi.mock("@/lib/downloadToolArtifact", () => ({
  downloadToolArtifact: (...args: unknown[]) => downloadToolArtifact(...args),
  fetchToolArtifact: (...args: unknown[]) => fetchToolArtifact(...args),
}));

import { MessageBubble } from "./MessageBubble";
import { mapResponseToMessage } from "@/hooks/useChat";
import type { ChatMessage } from "@/hooks/useChat";
import type { MessageResponse } from "@/types/api";
import type { ThinkingStep } from "@/types/thinking";

const step = (over: Partial<ThinkingStep>): ThinkingStep => ({
  id: "s",
  title: "t",
  tool: "create_file",
  status: "done",
  ...over,
});

const assistant = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: "a1",
  role: "assistant",
  content: "Готово! Вот документ.",
  ...over,
});

describe("MessageBubble tool artifact card", () => {
  it("renders one artifact card derived from done tool steps", () => {
    render(
      <MessageBubble
        message={assistant({
          thinkingSteps: [
            step({ id: "tc-0-0", tool: "create_file", filename: "python_exam_prep.md" }),
            step({ id: "tc-1-0", tool: "str_replace", filename: "python_exam_prep.md" }),
          ],
        })}
        conversationId="conv-1"
        isLast
      />,
    );
    expect(screen.getByText("Python exam prep")).toBeInTheDocument();
    expect(screen.getByText("Документ · MD")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Скачать/ })).toBeInTheDocument();
  });

  it("suppresses the light-path heuristic card when the message has tool artifacts (tr-v0l)", () => {
    // Live (regenerate): a message with real tool artifacts ALSO rendered a
    // light-path heuristic card for the same content (model wrote the doc inline
    // too) → 2–3 cards for one file. With tool artifacts present, the heuristic
    // must be skipped: exactly ONE «Открыть документ» button — the tool card
    // («Exam prep») — and NOT the heuristic card built from the inline heading
    // («Большой документ»).
    const msg = assistant({
      content: [
        "Готово, файл создан:",
        "",
        "```markdown",
        "# Большой документ",
        "",
        "Текст документа с примерами.",
        "```",
      ].join("\n"),
      thinkingSteps: [
        step({ id: "tc-0-0", tool: "create_file", filename: "exam_prep.md" }),
      ],
    });
    render(<MessageBubble message={msg} conversationId="conv-1" isLast />);
    // No heuristic card for the inline heading.
    expect(
      screen.queryByRole("button", { name: /Открыть документ: Большой документ/ }),
    ).toBeNull();
    // Exactly one openable card — the tool card.
    const openButtons = screen.getAllByRole("button", {
      name: /Открыть документ/,
    });
    expect(openButtons).toHaveLength(1);
    expect(
      screen.getByRole("button", { name: /Открыть документ: Exam prep/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("Exam prep")).toBeInTheDocument();
    expect(screen.getByText("Документ · MD")).toBeInTheDocument();
  });

  it("opens the canvas with the fetched content when the card body is clicked (tr-lqn)", async () => {
    downloadToolArtifact.mockClear();
    fetchToolArtifact.mockClear();
    const onOpenArtifact = vi.fn();
    render(
      <MessageBubble
        message={assistant({
          thinkingSteps: [
            step({ id: "tc-0-0", tool: "create_file", filename: "python_exam_prep.md" }),
          ],
        })}
        conversationId="conv-1"
        onOpenArtifact={onOpenArtifact}
        isLast
      />,
    );
    // The card body is its own button (separate from «Скачать»).
    await userEvent.click(
      screen.getByRole("button", { name: /Открыть документ: Python exam prep/ }),
    );
    await waitFor(() => expect(onOpenArtifact).toHaveBeenCalledTimes(1));
    // Content was fetched from the per-conversation artifact endpoint.
    expect(fetchToolArtifact).toHaveBeenCalledWith("conv-1", "python_exam_prep.md");
    // Clicking the body must NOT trigger a download.
    expect(downloadToolArtifact).not.toHaveBeenCalled();
    // The artifact handed to the canvas carries the fetched content + a markdown kind.
    expect(onOpenArtifact.mock.calls[0][0]).toMatchObject({
      kind: "markdown",
      title: "Python exam prep",
      content: "# Python exam prep\n\nСодержимое документа.",
      source: "server",
    });
  });

  it("download button does NOT open the canvas (stopPropagation) (tr-lqn)", async () => {
    downloadToolArtifact.mockClear();
    fetchToolArtifact.mockClear();
    const onOpenArtifact = vi.fn();
    render(
      <MessageBubble
        message={assistant({
          thinkingSteps: [
            step({ id: "tc-0-0", tool: "create_file", filename: "python_exam_prep.md" }),
          ],
        })}
        conversationId="conv-1"
        onOpenArtifact={onOpenArtifact}
        isLast
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /Скачать/ }));
    await waitFor(() => expect(downloadToolArtifact).toHaveBeenCalledTimes(1));
    expect(onOpenArtifact).not.toHaveBeenCalled();
    expect(fetchToolArtifact).not.toHaveBeenCalled();
  });

  it("renders no card when no tool step is done", () => {
    render(
      <MessageBubble
        message={assistant({
          thinkingSteps: [
            step({ id: "r", filename: "draft.md", status: "running" }),
          ],
        })}
        conversationId="conv-1"
        isLast
      />,
    );
    expect(screen.queryByRole("button", { name: /Скачать/ })).toBeNull();
  });

  it("renders no card for an assistant turn without tool steps", () => {
    render(<MessageBubble message={assistant()} conversationId="conv-1" isLast />);
    expect(screen.queryByRole("button", { name: /Скачать/ })).toBeNull();
  });

  it("collapses the answer for an empty-content restored Stop — card restores, no phantom bubble (tr-tc0)", () => {
    // A tool-only hard Stop persists an assistant row with content="" (the tool
    // timeline + artifact carry the turn). The borderless answer block must NOT
    // render for empty content (no ghost element), while the «Скачать» card still
    // restores. Same tool steps in both renders, so the only `.prose` difference is
    // the answer body itself.
    const steps = [step({ id: "tc-0-0", tool: "create_file", filename: "big.md" })];

    const withText = render(
      <MessageBubble
        message={assistant({ content: "Готово", thinkingSteps: steps })}
        conversationId="conv-1"
        isLast
      />,
    );
    expect(withText.container.querySelector(".prose")).not.toBeNull(); // answer renders
    withText.unmount();

    const empty = render(
      <MessageBubble
        message={assistant({ content: "", thinkingSteps: steps })}
        conversationId="conv-1"
        isLast
      />,
    );
    expect(empty.container.querySelector(".prose")).toBeNull(); // no phantom answer bubble
    expect(screen.getByRole("button", { name: /Скачать/ })).toBeInTheDocument(); // card restores
  });

  it("full reload chain: a restored empty-content Stop record maps + renders the card, no phantom (tr-tc0)", () => {
    // Exactly what GET conversation messages returns after a tool-only hard Stop:
    // content="" with persisted done tool_steps. Drive the REAL reload mapper
    // (mapResponseToMessage → ThinkingProcess + ToolArtifactCard), not a hand-built
    // ChatMessage — so the full restore chain is exercised without OpenRouter.
    const restored: MessageResponse = {
      id: "m-restored",
      role: "assistant",
      content: "",
      model_id: "openai/gpt-4.1-mini",
      cost_micro_rub: 1234,
      created_at: "2026-06-15T00:00:00Z",
      is_active: true,
      tool_steps: [
        {
          id: "tc-0-0",
          tool: "create_file",
          title: "Создание файла big.md",
          filename: "big.md",
          status: "done",
        },
      ],
    };
    const { container } = render(
      <MessageBubble
        message={mapResponseToMessage(restored)}
        conversationId="conv-1"
        isLast
      />,
    );
    // Timeline + artifact card restored from the persisted tool_steps:
    expect(screen.getByRole("button", { name: /Скачать/ })).toBeInTheDocument();
    // No phantom answer bubble for the restored empty content:
    expect(container.querySelector(".prose")).toBeNull();
  });
});
