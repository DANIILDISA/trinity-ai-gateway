import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ApiRequestError } from "@/lib/api";

// The page under test composes heavy children; probe ChatWindow's props via a
// stub so the test asserts the page-level wiring (error text, kept input),
// not the composer internals.
type ProbeProps = {
  error: string | null;
  inputValue: string;
  onInputChange(v: string): void;
  onSend(images: unknown[], files: unknown[]): void;
  onClearError(): void;
};
vi.mock("@/components/chat/ChatWindow", () => ({
  ChatWindow: (props: ProbeProps) => (
    <div>
      <div data-testid="probe-error">{props.error ?? ""}</div>
      <div data-testid="probe-input">{props.inputValue}</div>
      <button onClick={() => props.onInputChange("привет из теста")}>type</button>
      <button onClick={() => props.onSend([], [])}>send</button>
      <button onClick={() => props.onClearError()}>clear</button>
    </div>
  ),
}));

const createConversation = vi.fn();
const sendMessage = vi.fn();
const clearError = vi.fn();

vi.mock("@/components/chat/ChatSessionProvider", () => ({
  useChatSession: () => ({
    messages: [],
    isStreaming: false,
    error: null,
    sendMessage,
    stop: vi.fn(),
    clearError,
    webSearchEnabled: false,
    toggleWebSearch: vi.fn(),
    thinkingEnabled: false,
    thinkingEffort: "medium",
    toggleThinking: vi.fn(),
    setThinkingEffort: vi.fn(),
    toolsEnabled: false,
    toggleTools: vi.fn(),
    createConversation,
    activeConversationId: null,
    setActiveConversationId: vi.fn(),
    selectedModelId: "model-x",
    setSelectedModelId: vi.fn(),
    conversationImages: new Map(),
    toggleImageContext: vi.fn(),
    conversationFiles: new Map(),
    toggleFileContext: vi.fn(),
    conversationVersions: new Map(),
    loadConversationVersions: vi.fn(),
    switchVersion: vi.fn(),
    regenerate: vi.fn(),
    editAndResend: vi.fn(),
    isSidebarOpen: true,
  }),
}));

vi.mock("@/components/chat/ModelSelector", () => ({
  useModels: () => ({ models: [{ id: "model-x", status: "available" }] }),
}));

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: null }),
}));

import NewChatPage from "./page";

beforeEach(() => {
  createConversation.mockReset();
  sendMessage.mockReset();
  clearError.mockReset();
});

describe("NewChatPage — conversation-create failure UX (tr-w4fy.2)", () => {
  it("shows an in-app RU error (no window.alert) and KEEPS the typed input", async () => {
    const alertSpy = vi
      .spyOn(window, "alert")
      .mockImplementation(() => undefined);
    createConversation.mockRejectedValue(
      new ApiRequestError(
        "Сервер перегружен, попробуйте позже",
        503,
        "redis_unavailable",
      ),
    );

    render(<NewChatPage />);
    const user = userEvent.setup();
    await user.click(screen.getByText("type"));
    await user.click(screen.getByText("send"));

    await waitFor(() =>
      expect(screen.getByTestId("probe-error").textContent).toBe(
        "Сервер перегружен, попробуйте позже",
      ),
    );
    // The blocking native alert must be gone.
    expect(alertSpy).not.toHaveBeenCalled();
    // The user's message survives the failure — no retyping.
    expect(screen.getByTestId("probe-input").textContent).toBe(
      "привет из теста",
    );
    expect(sendMessage).not.toHaveBeenCalled();
    alertSpy.mockRestore();
  });

  it("clears the create error via onClearError", async () => {
    createConversation.mockRejectedValue(
      new ApiRequestError("Сервер перегружен, попробуйте позже", 503),
    );
    render(<NewChatPage />);
    const user = userEvent.setup();
    await user.click(screen.getByText("type"));
    await user.click(screen.getByText("send"));
    await waitFor(() =>
      expect(screen.getByTestId("probe-error").textContent).not.toBe(""),
    );

    await user.click(screen.getByText("clear"));
    expect(screen.getByTestId("probe-error").textContent).toBe("");
  });

  it("clears the input only after the conversation exists (happy path)", async () => {
    createConversation.mockResolvedValue("conv-9");
    sendMessage.mockResolvedValue(undefined);
    render(<NewChatPage />);
    const user = userEvent.setup();
    await user.click(screen.getByText("type"));
    await user.click(screen.getByText("send"));

    await waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(1));
    expect(sendMessage.mock.calls[0][0]).toBe("привет из теста");
    expect(sendMessage.mock.calls[0][2]).toBe("conv-9");
    expect(screen.getByTestId("probe-input").textContent).toBe("");
  });
});
