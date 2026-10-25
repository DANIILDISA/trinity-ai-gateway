import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";

import { ChatSidebar, type ChatSidebarProps } from "./ChatSidebar";
import type { ConversationSummary } from "@/hooks/useConversations";

function makeConvs(n: number): ConversationSummary[] {
  return Array.from({ length: n }, (_, i) => ({
    id: `id-${i}`,
    title: `Чат ${String(i).padStart(2, "0")}`,
    model_id: "openai/gpt-4.1-mini",
    updated_at: new Date(Date.UTC(2026, 0, 1, 0, 0, n - i)).toISOString(),
  }));
}

function renderSidebar(overrides: Partial<ChatSidebarProps> = {}) {
  const props: ChatSidebarProps = {
    conversations: makeConvs(25),
    isLoading: false,
    currentConversationId: null,
    isOpen: true,
    userName: "Тест",
    userEmail: "t@e.ru",
    pinnedIds: new Set(),
    onToggle: vi.fn(),
    onNewChat: vi.fn(),
    onTogglePin: vi.fn(),
    onDelete: vi.fn(),
    onRename: vi.fn(),
    onLogout: vi.fn(),
    archived: [],
    isLoadingArchived: false,
    onLoadArchived: vi.fn(),
    onRestore: vi.fn(),
    onPermanentDelete: vi.fn(),
    onOpenAll: vi.fn(),
    ...overrides,
  };
  render(<ChatSidebar {...props} />);
  return props;
}

describe("ChatSidebar recents", () => {
  it("renders only the 20 most recent non-pinned chats", () => {
    renderSidebar();
    expect(screen.getByText("Чат 00")).toBeInTheDocument();
    expect(screen.getByText("Чат 19")).toBeInTheDocument();
    expect(screen.queryByText("Чат 20")).not.toBeInTheDocument();
    expect(screen.queryByText("Чат 24")).not.toBeInTheDocument();
  });

  it("shows «Показать все» and calls onOpenAll on click", () => {
    const props = renderSidebar();
    fireEvent.click(screen.getByText("Показать все"));
    expect(props.onOpenAll).toHaveBeenCalledTimes(1);
  });

  it("confirms via a styled modal before deleting, only deleting when accepted (tr-zrlp/tr-v42t)", () => {
    const props = renderSidebar();
    const firstMenuButton = () =>
      screen.getAllByLabelText("Действия с диалогом")[0];

    // Open the row menu and click delete → a styled confirm modal appears
    // (the native window.confirm was replaced by ConfirmDialog, tr-v42t).
    fireEvent.click(firstMenuButton());
    fireEvent.click(screen.getByText("Удалить"));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Удалить диалог?");
    expect(props.onDelete).not.toHaveBeenCalled();

    // Decline → the dialog closes and nothing is deleted.
    fireEvent.click(within(dialog).getByText("Отмена"));
    expect(props.onDelete).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    // Re-open and accept → delete fires exactly once.
    fireEvent.click(firstMenuButton());
    fireEvent.click(screen.getByText("Удалить"));
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Удалить" }),
    );
    expect(props.onDelete).toHaveBeenCalledTimes(1);
  });
});
