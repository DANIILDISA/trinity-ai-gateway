import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import { MessageBubble } from "./MessageBubble";
import type { ChatMessage } from "@/hooks/useChat";

const streaming = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: "a1",
  role: "assistant",
  content: "",
  isStreaming: true,
  ...over,
});

describe("MessageBubble — streaming loading indicator", () => {
  it("does NOT render the three-dot ellipsis while streaming with no content yet", () => {
    // The animated Trinity neural mark is the sole loading indicator now — the
    // redundant bouncing-dots ellipsis is gone.
    render(<MessageBubble message={streaming()} isLast />);
    expect(screen.queryByLabelText("Генерация ответа")).toBeNull();
  });

  it("keeps the animated Trinity neural mark as the streaming-loading indicator", () => {
    // The NeuralIcon (rendered for the last assistant turn) still covers the
    // streaming-no-content state, so removing the dots doesn't leave a blank.
    const { container } = render(<MessageBubble message={streaming()} isLast />);
    // NeuralIcon emits an inline <svg> with the emerald neural keyframes.
    expect(container.querySelector("svg")).not.toBeNull();
  });

  it("renders no phantom answer bubble while streaming with empty content (tr-tc0)", () => {
    const { container } = render(<MessageBubble message={streaming()} isLast />);
    // No prose/markdown answer body until the first token arrives.
    expect(container.querySelector(".prose")).toBeNull();
  });
});
