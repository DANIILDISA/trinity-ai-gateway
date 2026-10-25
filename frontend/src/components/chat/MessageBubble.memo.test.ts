import { describe, it, expect } from "vitest";

import { messagePropsEqual, type MessageBubbleProps } from "./MessageBubble";
import type { ChatMessage } from "@/hooks/useChat";

const msg = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: "a1",
  role: "assistant",
  content: "hi",
  ...over,
});

const props = (m: ChatMessage): MessageBubbleProps => ({ message: m, isLast: false });

describe("messagePropsEqual (MessageBubble memo comparator)", () => {
  it("treats identical-content thinkingSteps with a NEW array ref as equal (H7)", () => {
    // useChat snapshots a FRESH thinkingSteps array every 50ms flush; a reference
    // compare forced a full re-render 20–40×/sec even when nothing changed.
    const steps = [
      { id: "s1", tool: "create_file", title: "t", status: "done" as const, filename: "a.md" },
    ];
    const a = msg({ thinkingSteps: steps });
    const b = msg({ thinkingSteps: steps.map((s) => ({ ...s })) });
    expect(messagePropsEqual(props(a), props(b))).toBe(true);
  });

  it("re-renders when a tool step's status changes", () => {
    const a = msg({ thinkingSteps: [{ id: "s1", tool: "create_file", title: "t", status: "running" as const }] });
    const b = msg({ thinkingSteps: [{ id: "s1", tool: "create_file", title: "t", status: "done" as const }] });
    expect(messagePropsEqual(props(a), props(b))).toBe(false);
  });

  it("re-renders when thinkingSegments change (was omitted from the comparator)", () => {
    const a = msg({ thinkingSegments: [{ id: "seg-0", text: "x", summaryPending: true }] });
    const b = msg({ thinkingSegments: [{ id: "seg-0", text: "x", summary: "Готово", summaryPending: false }] });
    expect(messagePropsEqual(props(a), props(b))).toBe(false);
  });

  it("treats identical-content thinkingSegments with a NEW array ref as equal", () => {
    const segs = [{ id: "seg-0", text: "Думаю", summary: "Анализ", summaryPending: false }];
    const a = msg({ thinkingSegments: segs });
    const b = msg({ thinkingSegments: segs.map((s) => ({ ...s })) });
    expect(messagePropsEqual(props(a), props(b))).toBe(true);
  });

  it("re-renders when content changes", () => {
    expect(messagePropsEqual(props(msg({ content: "a" })), props(msg({ content: "b" })))).toBe(false);
  });
});
