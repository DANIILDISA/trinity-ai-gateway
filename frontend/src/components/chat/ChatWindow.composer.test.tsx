import { describe, it, expect, vi } from "vitest";
import { act, render, screen, fireEvent } from "@testing-library/react";

import { ChatWindow, MAX_MESSAGE_CHARS, type ChatWindowProps } from "./ChatWindow";
import type { ModelInfo } from "@/types/api";

const model = (over: Partial<ModelInfo> = {}): ModelInfo => ({
  id: "m1",
  name: "Claude Haiku",
  provider: "anthropic",
  input_price_per_m: "100",
  output_price_per_m: "300",
  context_window: 200000,
  status: "active",
  supports_image_input: false,
  supports_file_input: false,
  supports_tools: true,
  supports_reasoning: false,
  supports_web_search: false,
  ...over,
});

const baseProps = (over: Partial<ChatWindowProps> = {}): ChatWindowProps => ({
  messages: [],
  isStreaming: false,
  error: null,
  models: [model()],
  selectedModelId: "m1",
  inputValue: "",
  isLoadingHistory: false,
  webSearchEnabled: false,
  thinkingEnabled: false,
  thinkingEffort: "medium",
  toolsEnabled: false,
  conversationImages: new Map(),
  conversationFiles: new Map(),
  sidebarOpen: true,
  onToggleImageContext: vi.fn(),
  onToggleFileContext: vi.fn(),
  onModelChange: vi.fn(),
  onInputChange: vi.fn(),
  onSend: vi.fn(),
  onStop: vi.fn(),
  onClearError: vi.fn(),
  onToggleWebSearch: vi.fn(),
  onToggleThinking: vi.fn(),
  onSetThinkingEffort: vi.fn(),
  onToggleTools: vi.fn(),
  ...over,
});

const getTextarea = () =>
  screen.getByPlaceholderText("Напишите сообщение...") as HTMLTextAreaElement;
const getSendButton = () => screen.getByRole("button", { name: "Отправить" });

describe("ChatWindow composer — geometry (tr-cy0.6.14 / tr-cy0.6.15)", () => {
  // tr-cy0.6.14: the composer box must read less round (more square) than the
  // old pill-like 28px radius, matching the reference composer.
  it("uses a less-rounded composer container, not the old 28px pill", () => {
    render(<ChatWindow {...baseProps()} />);
    // No attachments → the textarea's parent IS the composer box wrapper.
    const box = getTextarea().parentElement as HTMLElement;
    expect(box.className).toContain("rounded-2xl");
    expect(box.className).not.toContain("rounded-[28px]");
  });

  // tr-cy0.6.15: the placeholder must not float — symmetric vertical padding +
  // a tighter resting height balance the gap above the text against the gap to
  // the "+" row below it.
  it("balances the textarea vertical padding (symmetric, not top-only)", () => {
    render(<ChatWindow {...baseProps()} />);
    const ta = getTextarea();
    expect(ta.className).toContain("py-2.5");
    expect(ta.className).toContain("min-h-[44px]");
    expect(ta.className).not.toContain("pt-3");
    expect(ta.className).not.toContain("min-h-[48px]");
  });
});

describe("ChatWindow composer — 32000-char send guard (tr-q5d / F-03)", () => {
  it("mirrors the backend _MAX_TEXT_LEN cap", () => {
    expect(MAX_MESSAGE_CHARS).toBe(32000);
  });

  it("disables send and shows a hint when the input exceeds the limit", () => {
    const onSend = vi.fn();
    render(
      <ChatWindow
        {...baseProps({ inputValue: "x".repeat(MAX_MESSAGE_CHARS + 1), onSend })}
      />,
    );

    expect(getSendButton()).toBeDisabled();
    // ru-RU groups the number (e.g. "32 000" with a no-break space), so match
    // the limit digits tolerantly rather than as a bare "32000".
    expect(
      screen.getByText(/Сообщение слишком длинное: максимум 32\s?000 символов/),
    ).toBeInTheDocument();

    // Enter must not submit while over the limit.
    fireEvent.keyDown(getTextarea(), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("enables send exactly at the limit", () => {
    const onSend = vi.fn();
    render(
      <ChatWindow
        {...baseProps({ inputValue: "x".repeat(MAX_MESSAGE_CHARS), onSend })}
      />,
    );

    const send = getSendButton();
    expect(send).not.toBeDisabled();
    expect(
      screen.queryByText(/Сообщение слишком длинное/),
    ).not.toBeInTheDocument();

    fireEvent.keyDown(getTextarea(), { key: "Enter" });
    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("keeps send enabled and the limit hint hidden for a normal-length message", () => {
    const onSend = vi.fn();
    render(<ChatWindow {...baseProps({ inputValue: "hello", onSend })} />);

    expect(getSendButton()).not.toBeDisabled();
    expect(
      screen.queryByText(/Сообщение слишком длинное/),
    ).not.toBeInTheDocument();

    fireEvent.keyDown(getTextarea(), { key: "Enter" });
    expect(onSend).toHaveBeenCalledTimes(1);
  });
});

describe("ChatWindow composer — same-tick double-submit lock (tr-n5b / MONEY)", () => {
  // Dispatch two Enter keydowns on the textarea WITHIN A SINGLE React `act()`
  // batch — i.e. the same JS tick, before React can re-render and disable the
  // composer. This is the exact race that fires two POST /messages and bills
  // the user twice. A React useState flag updates too late to stop it; only a
  // synchronous ref lock at the top of `submit` can.
  const fireEnterTwiceSameTick = (ta: HTMLTextAreaElement) => {
    act(() => {
      ta.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
      );
      ta.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
      );
    });
  };

  it("fires onSend exactly ONCE for two same-tick Enter keydowns (fresh chat)", () => {
    const onSend = vi.fn();
    // inputValue stays controlled (parent mock doesn't clear it), mirroring the
    // window where the composer hasn't re-rendered yet between the two keydowns.
    render(<ChatWindow {...baseProps({ inputValue: "hello", onSend })} />);

    fireEnterTwiceSameTick(getTextarea());

    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("fires onSend exactly ONCE for two same-tick send-button clicks", () => {
    const onSend = vi.fn();
    render(<ChatWindow {...baseProps({ inputValue: "hello", onSend })} />);

    const btn = getSendButton();
    act(() => {
      btn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      btn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("still sends once on a normal single submit", () => {
    const onSend = vi.fn();
    render(<ChatWindow {...baseProps({ inputValue: "hello", onSend })} />);

    fireEvent.keyDown(getTextarea(), { key: "Enter" });

    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("clears the lock after a send settles so the next message sends again", () => {
    const onSend = vi.fn();
    const { rerender } = render(
      <ChatWindow {...baseProps({ inputValue: "first", onSend })} />,
    );

    // First message: same-tick double → one send.
    fireEnterTwiceSameTick(getTextarea());
    expect(onSend).toHaveBeenCalledTimes(1);

    // Send settles: the parent clears the input, then the user types again.
    // Both transitions re-render the composer, which must release the lock.
    rerender(<ChatWindow {...baseProps({ inputValue: "", onSend })} />);
    rerender(<ChatWindow {...baseProps({ inputValue: "second", onSend })} />);

    fireEvent.keyDown(getTextarea(), { key: "Enter" });
    expect(onSend).toHaveBeenCalledTimes(2);
  });

  it("releases the lock after an error so the user can retry", () => {
    const onSend = vi.fn();
    const { rerender } = render(
      <ChatWindow {...baseProps({ inputValue: "oops", onSend })} />,
    );

    fireEnterTwiceSameTick(getTextarea());
    expect(onSend).toHaveBeenCalledTimes(1);

    // Send failed: input was cleared by the parent and an error surfaced, but
    // isStreaming never went true. The composer must still accept a retry.
    rerender(
      <ChatWindow
        {...baseProps({ inputValue: "", onSend, error: "boom" })}
      />,
    );
    rerender(
      <ChatWindow
        {...baseProps({ inputValue: "retry", onSend, error: "boom" })}
      />,
    );

    fireEvent.keyDown(getTextarea(), { key: "Enter" });
    expect(onSend).toHaveBeenCalledTimes(2);
  });

  it("releases the lock when an error surfaces WITHOUT the input changing (tr-w4fy.2)", () => {
    // Conversation-create failure keeps the composed text so the user retries
    // with one click. isStreaming never goes true and inputValue never changes —
    // the error itself must release the lock, or every later send is a silent
    // no-op until reload.
    const onSend = vi.fn();
    const { rerender } = render(
      <ChatWindow {...baseProps({ inputValue: "оставь текст", onSend })} />,
    );

    fireEvent.keyDown(getTextarea(), { key: "Enter" });
    expect(onSend).toHaveBeenCalledTimes(1);

    rerender(
      <ChatWindow
        {...baseProps({
          inputValue: "оставь текст",
          onSend,
          error: "Сервер перегружен, попробуйте позже",
        })}
      />,
    );

    fireEvent.keyDown(getTextarea(), { key: "Enter" });
    expect(onSend).toHaveBeenCalledTimes(2);
  });
});
