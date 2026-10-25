import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import { MessageBubble } from "./MessageBubble";
import { mapResponseToMessage } from "@/hooks/useChat";
import type { ChatMessage } from "@/hooks/useChat";
import type { MessageResponse } from "@/types/api";

const assistant = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: "a1",
  role: "assistant",
  content: "Частичный ответ, который оборвали",
  actualCost: "0.0012",
  requestId: "req-1",
  ...over,
});

describe("MessageBubble — stopped marker (tr-fo1)", () => {
  it("shows an inline «Остановлено» marker on a thinking-off content stop (no timeline)", () => {
    // No thinking / no tool steps ⇒ no ThinkingProcess timeline renders, so the
    // signal must live on the bubble meta row instead.
    render(<MessageBubble message={assistant({ stopped: true })} isLast />);
    expect(screen.getByText("Остановлено")).toBeInTheDocument();
  });

  it("does NOT show the «Остановлено» marker on a completed reply", () => {
    render(<MessageBubble message={assistant({ stopped: false })} isLast />);
    expect(screen.queryByText("Остановлено")).toBeNull();
  });

  it("renders «Остановлено» in the timeline (not «Готово») for a stopped thinking reply", () => {
    render(
      <MessageBubble
        message={assistant({
          stopped: true,
          thinking: "обдумываю и обрываю",
          thinkingDurationMs: 4000,
        })}
        isLast
      />,
    );
    expect(screen.getByText("Остановлено")).toBeInTheDocument();
    expect(screen.queryByText(/^Готово/)).toBeNull();
  });

  it("full reload chain: a partial usage_log maps stopped=true through mapResponseToMessage", () => {
    const restored: MessageResponse = {
      id: "m-restored",
      role: "assistant",
      content: "оборванный ответ",
      model_id: "openai/gpt-4.1-mini",
      cost_micro_rub: 1234,
      created_at: "2026-06-15T00:00:00Z",
      is_active: true,
      request_id: "req-stopped",
      stopped: true,
    };
    render(<MessageBubble message={mapResponseToMessage(restored)} isLast />);
    expect(screen.getByText("Остановлено")).toBeInTheDocument();
  });
});
