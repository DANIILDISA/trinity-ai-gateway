"use client";

import { useState } from "react";
import { useParams, useRouter } from "next/navigation";

import { ChatSidebar } from "@/components/chat/ChatSidebar";
import { ChatsOverlay } from "@/components/chat/ChatsOverlay";
import {
  ChatSessionProvider,
  useChatSession,
} from "@/components/chat/ChatSessionProvider";
import { useAuth } from "@/hooks/useAuth";
import { useToast } from "@/components/ui/Toast";

export default function ChatLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <ChatSessionProvider>
      <ChatLayoutInner>{children}</ChatLayoutInner>
    </ChatSessionProvider>
  );
}

function PanelToggleIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect
        x="3"
        y="4"
        width="18"
        height="16"
        rx="2"
        stroke="currentColor"
        strokeWidth="2"
      />
      <path d="M9 4v16" stroke="currentColor" strokeWidth="2" />
    </svg>
  );
}

function ChatLayoutInner({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const params = useParams<{ id?: string }>();
  const routeConversationId = params?.id ?? null;
  const { user, logout } = useAuth();
  const toast = useToast();
  const [isChatsOpen, setIsChatsOpen] = useState(false);

  const {
    conversations,
    isLoadingConversations,
    deleteConversation,
    renameConversation,
    stop,
    setInitialMessages,
    clearConversationImages,
    clearConversationFiles,
    activeConversationId,
    setActiveConversationId,
    archivedConversations,
    isLoadingArchived,
    loadArchived,
    restoreConversation,
    permanentDeleteConversation,
    isSidebarOpen,
    toggleSidebar,
    pinnedIds,
    togglePin,
    loadAllConversations,
    isLoadingAllConversations,
  } = useChatSession();

  // BUG 9: a new chat must be startable at any time, including mid-stream.
  // Abort the active stream, clear the transcript, and route to the fresh view.
  const handleNewChat = () => {
    stop();
    setInitialMessages([]);
    clearConversationImages();
    clearConversationFiles();
    setActiveConversationId(null);
    router.push("/chat");
  };

  const handleDelete = async (id: string) => {
    try {
      await deleteConversation(id);
      if (id === routeConversationId || id === activeConversationId) {
        setInitialMessages([]);
        setActiveConversationId(null);
        router.push("/chat");
      }
    } catch (err) {
      window.alert(err instanceof Error ? err.message : "Ошибка удаления.");
    }
  };

  const handleRename = async (id: string, title: string) => {
    try {
      await renameConversation(id, title);
    } catch (err) {
      window.alert(
        err instanceof Error ? err.message : "Ошибка переименования.",
      );
    }
  };

  const handleRestore = async (id: string) => {
    try {
      await restoreConversation(id);
    } catch (err) {
      window.alert(err instanceof Error ? err.message : "Ошибка восстановления.");
    }
  };

  const handlePermanentDelete = async (id: string) => {
    try {
      await permanentDeleteConversation(id);
      if (id === routeConversationId || id === activeConversationId) {
        setInitialMessages([]);
        setActiveConversationId(null);
        router.push("/chat");
      }
    } catch (err) {
      window.alert(err instanceof Error ? err.message : "Ошибка удаления.");
    }
  };

  const handleBulkDelete = async (ids: string[]) => {
    let failures = 0;
    for (const id of ids) {
      try {
        await deleteConversation(id);
      } catch {
        failures += 1;
      }
    }
    if (
      ids.includes(routeConversationId ?? "") ||
      ids.includes(activeConversationId ?? "")
    ) {
      setInitialMessages([]);
      setActiveConversationId(null);
      router.push("/chat");
    }
    if (failures > 0) {
      toast.error(`Не удалось удалить чатов: ${failures}`);
      void loadAllConversations();
    }
  };

  return (
    <div className="flex h-screen overflow-hidden bg-[var(--trinity-canvas)]">
      <ChatSidebar
        conversations={conversations}
        isLoading={isLoadingConversations}
        currentConversationId={routeConversationId ?? activeConversationId}
        isOpen={isSidebarOpen}
        userName={user?.display_name ?? null}
        userEmail={user?.email ?? null}
        userBalance={user?.balance ?? null}
        userReserved={user?.reserved ?? null}
        userAvailable={user?.available ?? null}
        pinnedIds={pinnedIds}
        onToggle={toggleSidebar}
        onNewChat={handleNewChat}
        onTogglePin={togglePin}
        onDelete={(id) => void handleDelete(id)}
        onRename={(id, title) => void handleRename(id, title)}
        onLogout={() => void logout()}
        archived={archivedConversations}
        isLoadingArchived={isLoadingArchived}
        onLoadArchived={() => void loadArchived()}
        onRestore={(id) => void handleRestore(id)}
        onPermanentDelete={(id) => void handlePermanentDelete(id)}
        onOpenAll={() => setIsChatsOpen(true)}
      />

      {/* Mobile backdrop: below md the sidebar is a fixed drawer over the chat;
          this dims the chat and closes the drawer on tap-outside (tr-rwrt). On
          md+ the sidebar is an in-flow column, so the backdrop is hidden. */}
      {isSidebarOpen ? (
        <div
          className="fixed inset-0 z-40 bg-black/60 md:hidden"
          onClick={toggleSidebar}
          aria-hidden="true"
        />
      ) : null}

      {/* Fixed re-open toggle, visible only while the sidebar is collapsed. */}
      {isSidebarOpen ? null : (
        <button
          type="button"
          onClick={toggleSidebar}
          title="Показать панель"
          aria-label="Показать панель"
          className="fixed left-3 top-3 z-40 inline-flex h-9 w-9 items-center justify-center rounded-lg text-neutral-400 transition-colors hover:bg-neutral-800 hover:text-neutral-100"
        >
          <PanelToggleIcon />
        </button>
      )}

      <div className="relative flex min-w-0 flex-1">
        {children}
        {isChatsOpen ? (
          <ChatsOverlay
            conversations={conversations}
            pinnedIds={pinnedIds}
            isLoading={isLoadingAllConversations}
            currentConversationId={routeConversationId ?? activeConversationId}
            onClose={() => setIsChatsOpen(false)}
            onOpenChat={(id) => {
              setIsChatsOpen(false);
              router.push(`/chat/${id}`);
            }}
            onNewChat={() => {
              setIsChatsOpen(false);
              handleNewChat();
            }}
            onTogglePin={togglePin}
            onRename={(id, title) => void handleRename(id, title)}
            onDelete={(id) => void handleDelete(id)}
            onBulkDelete={(ids) => void handleBulkDelete(ids)}
            onLoadAll={() => void loadAllConversations()}
          />
        ) : null}
      </div>
    </div>
  );
}
