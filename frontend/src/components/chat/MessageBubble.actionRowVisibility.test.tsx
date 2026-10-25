import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import { MessageBubble } from "./MessageBubble";
import type { ChatMessage } from "@/hooks/useChat";

const assistant = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: "a1",
  role: "assistant",
  content: "Готовый ответ ассистента",
  actualCost: "0.0012",
  requestId: "req-1",
  ...over,
});

// The action/meta row is the element wrapping the copy/regenerate controls. We
// locate it from the «Скопировать ответ» button (always present for a completed
// answer) and walk up to its container. jsdom has no real hover, so we assert on
// the row's className instead of simulating a pointer.
function actionRow(): HTMLElement {
  const copyBtn = screen.getByLabelText("Скопировать ответ");
  // The copy control sits inside a Tooltip wrapper; the action row is the
  // nearest ancestor carrying the transition-opacity class (present in both the
  // last and non-last branches), so closest() resolves it without depending on
  // the opacity value under test.
  const row = copyBtn.closest<HTMLElement>(".transition-opacity");
  if (!row) throw new Error("action row not found");
  return row;
}

describe("MessageBubble — last-answer action row visibility", () => {
  it("is always visible (opacity-100, no hover gating) on the LAST assistant answer", () => {
    render(<MessageBubble message={assistant()} isLast />);
    const row = actionRow();
    expect(row.className).toContain("opacity-100");
    expect(row.className).not.toContain("opacity-0");
    expect(row.className).not.toContain("group-hover/msg:opacity-100");
    // Transition is preserved.
    expect(row.className).toContain("transition-opacity");
  });

  it("stays hover-gated (opacity-0 + group-hover/msg:opacity-100) on NON-last answers", () => {
    render(<MessageBubble message={assistant()} isLast={false} />);
    const row = actionRow();
    expect(row.className).toContain("opacity-0");
    expect(row.className).toContain("group-hover/msg:opacity-100");
    expect(row.className).toContain("transition-opacity");
  });
});
