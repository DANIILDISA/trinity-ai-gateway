import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { ChatsOverlay, type ChatsOverlayProps } from "./ChatsOverlay";
import type { ConversationSummary } from "@/hooks/useConversations";

function makeConvs(n: number): ConversationSummary[] {
  return Array.from({ length: n }, (_, i) => ({
    id: `id-${i}`,
    title: `Чат ${String(i).padStart(2, "0")}`,
    model_id: "openai/gpt-4.1-mini",
    updated_at: new Date(Date.UTC(2026, 0, 1, 0, 0, n - i)).toISOString(),
  }));
}

function renderOverlay(overrides: Partial<ChatsOverlayProps> = {}) {
  const props: ChatsOverlayProps = {
    conversations: makeConvs(5),
    pinnedIds: new Set(),
    isLoading: false,
    currentConversationId: null,
    onClose: vi.fn(),
    onOpenChat: vi.fn(),
    onNewChat: vi.fn(),
    onTogglePin: vi.fn(),
    onRename: vi.fn(),
    onDelete: vi.fn(),
    onBulkDelete: vi.fn(),
    onLoadAll: vi.fn(),
    ...overrides,
  };
  render(<ChatsOverlay {...props} />);
  return props;
}

beforeEach(() => vi.clearAllMocks());

describe("ChatsOverlay", () => {
  it("requests the full list on mount", () => {
    const props = renderOverlay();
    expect(props.onLoadAll).toHaveBeenCalledTimes(1);
  });

  it("filters the list by the search query", () => {
    renderOverlay();
    fireEvent.change(screen.getByPlaceholderText("Поиск чатов…"), {
      target: { value: "Чат 03" },
    });
    expect(screen.getByText("Чат 03")).toBeInTheDocument();
    expect(screen.queryByText("Чат 00")).not.toBeInTheDocument();
  });

  it("shows «Ничего не найдено» when the search matches nothing", () => {
    renderOverlay();
    fireEvent.change(screen.getByPlaceholderText("Поиск чатов…"), {
      target: { value: "zzz" },
    });
    expect(screen.getByText("Ничего не найдено")).toBeInTheDocument();
  });

  it("opens a chat on row click", () => {
    const props = renderOverlay();
    fireEvent.click(screen.getByText("Чат 02"));
    expect(props.onOpenChat).toHaveBeenCalledWith("id-2");
  });

  it("closes on Escape", () => {
    const props = renderOverlay();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });

  it("closes via the ✕ button", () => {
    const props = renderOverlay();
    fireEvent.click(screen.getByLabelText("Закрыть"));
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });

  it("starts a new chat", () => {
    const props = renderOverlay();
    fireEvent.click(screen.getByText("Новый чат"));
    expect(props.onNewChat).toHaveBeenCalledTimes(1);
  });

  it("pinned filter shows only pinned chats", () => {
    renderOverlay({ pinnedIds: new Set(["id-1"]) });
    fireEvent.click(screen.getByText(/Фильтр:/));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Закреплённые" }));
    expect(screen.getByText("Чат 01")).toBeInTheDocument();
    expect(screen.queryByText("Чат 00")).not.toBeInTheDocument();
  });

  it("bulk-deletes the selected chats", () => {
    const props = renderOverlay();
    fireEvent.click(screen.getByText("Выбрать"));
    fireEvent.click(screen.getByLabelText("Выбрать «Чат 00»"));
    fireEvent.click(screen.getByLabelText("Выбрать «Чат 02»"));
    fireEvent.click(screen.getByText(/Удалить \(2\)/));
    expect(props.onBulkDelete).toHaveBeenCalledTimes(1);
    expect(props.onBulkDelete).toHaveBeenCalledWith(["id-0", "id-2"]);
  });
});
