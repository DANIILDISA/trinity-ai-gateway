import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("@/lib/highlight", () => ({
  highlightToHtml: vi.fn().mockResolvedValue(null),
}));

import { MessageBubble } from "./MessageBubble";
import type { ChatMessage } from "@/hooks/useChat";

const assistantWithDoc = (overrides: Partial<ChatMessage> = {}): ChatMessage => ({
  id: "a1",
  role: "assistant",
  content: [
    "Готово, вот документ:",
    "",
    "```markdown",
    "# Большой документ",
    "",
    "Текст документа с примерами.",
    "```",
    "",
    "Что думаешь?",
  ].join("\n"),
  ...overrides,
});

describe("MessageBubble artifacts", () => {
  it("renders an artifact card and keeps the stripped prose", () => {
    render(<MessageBubble message={assistantWithDoc()} isLast />);
    expect(
      screen.getByRole("button", { name: "Открыть документ: Большой документ" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Готово, вот документ:")).toBeInTheDocument();
    expect(screen.getByText("Что думаешь?")).toBeInTheDocument();
  });

  it("calls onOpenArtifact with the artifact when the card is clicked", async () => {
    const onOpenArtifact = vi.fn();
    render(
      <MessageBubble message={assistantWithDoc()} isLast onOpenArtifact={onOpenArtifact} />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Открыть документ: Большой документ" }),
    );
    expect(onOpenArtifact).toHaveBeenCalledTimes(1);
    expect(onOpenArtifact.mock.calls[0][0]).toMatchObject({
      kind: "markdown",
      title: "Большой документ",
    });
  });

  it("forms a generating card (download disabled) while the answer streams", () => {
    render(<MessageBubble message={assistantWithDoc({ isStreaming: true })} isLast />);
    expect(
      screen.getByRole("button", { name: "Открыть документ: Большой документ" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Скачать/ })).toBeDisabled();
  });

  it("keeps a short code block inline (no card) for a settled answer", () => {
    const msg: ChatMessage = {
      id: "a3",
      role: "assistant",
      content: ["Пример:", "", "```js", "const a = 1;", "```", "", "Конец."].join("\n"),
    };
    render(<MessageBubble message={msg} isLast />);
    expect(screen.queryByRole("button", { name: /Открыть документ/ })).toBeNull();
    expect(screen.getByText(/const a = 1;/)).toBeInTheDocument();
  });

  it("does not render the document body raw in the stream (only the card)", () => {
    render(<MessageBubble message={assistantWithDoc()} isLast />);
    expect(
      screen.getByRole("button", { name: "Открыть документ: Большой документ" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Текст документа с примерами.")).toBeNull();
  });
});
