"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";

import { dispatchBalanceRefresh } from "@/lib/balanceEvents";
import {
  useChat,
  type AttachedFile,
  type AttachedImage,
  type ChatMessage,
} from "@/hooks/useChat";
import {
  useConversations,
  type ConversationSummary,
} from "@/hooks/useConversations";
import type {
  ContextFile,
  ContextImage,
  MessageResponse,
  ThinkingEffort,
} from "@/types/api";

// Chat + conversation state lives at the layout level so it survives the
// /chat → /chat/[id] transition. Without this, sending the first message
// remounted the page and orphaned the in-flight SSE stream — the response
// never rendered until a manual refresh (BUG 1).
interface ChatSessionValue {
  messages: ChatMessage[];
  isStreaming: boolean;
  error: string | null;
  webSearchEnabled: boolean;
  toggleWebSearch(): void;
  thinkingEnabled: boolean;
  thinkingEffort: ThinkingEffort;
  toggleThinking(): void;
  setThinkingEffort(effort: ThinkingEffort): void;
  toolsEnabled: boolean;
  toggleTools(): void;
  applyConversationSettings(s: {
    thinkingEnabled?: boolean | null;
    thinkingEffort?: ThinkingEffort | null;
    toolsEnabled?: boolean | null;
    webSearchEnabled?: boolean | null;
  }): void;
  sendMessage(
    content: string,
    modelId: string,
    conversationId: string,
    images?: AttachedImage[],
    files?: AttachedFile[],
  ): Promise<void>;
  stop(): void;
  clearError(): void;
  setInitialMessages(messages: ChatMessage[]): void;

  // Branching (§6). The page sets the active conversation on load so the actions
  // target the right conversation; the actions stream new versions / repoint the
  // active path. conversationVersions is the lazily-loaded sibling cache.
  setActiveConversation(conversationId: string): void;
  regenerate(
    assistantId: string,
    opts?: {
      modelId?: string;
      thinkingEnabled?: boolean;
      thinkingEffort?: ThinkingEffort;
      webSearchEnabled?: boolean;
    },
  ): Promise<void>;
  editAndResend(
    userId: string,
    content: string,
    opts?: {
      modelId?: string;
      thinkingEnabled?: boolean;
      thinkingEffort?: ThinkingEffort;
      webSearchEnabled?: boolean;
      toolsEnabled?: boolean;
    },
  ): Promise<void>;
  switchVersion(nodeId: string): Promise<void>;
  conversationVersions: Map<string, MessageResponse[]>;
  loadConversationVersions(conversationId: string): Promise<void>;

  conversationImages: Map<string, ContextImage[]>;
  loadConversationImages(conversationId: string): Promise<void>;
  clearConversationImages(): void;
  toggleImageContext(imageId: string): void;

  conversationFiles: Map<string, ContextFile[]>;
  loadConversationFiles(conversationId: string): Promise<void>;
  clearConversationFiles(): void;
  toggleFileContext(fileId: string): void;

  conversations: ConversationSummary[];
  isLoadingConversations: boolean;
  createConversation(modelId: string): Promise<string>;
  deleteConversation(id: string): Promise<void>;
  renameConversation(id: string, title: string): Promise<void>;
  refreshConversations(): void;
  loadAllConversations(): Promise<void>;
  isLoadingAllConversations: boolean;
  allConversationsLoaded: boolean;

  archivedConversations: ConversationSummary[];
  isLoadingArchived: boolean;
  loadArchived(): Promise<void>;
  restoreConversation(id: string): Promise<void>;
  permanentDeleteConversation(id: string): Promise<void>;

  // Which conversation the chat state currently represents. Lets the
  // /chat/[id] page skip its history fetch when the provider already holds
  // the freshly streamed messages.
  activeConversationId: string | null;
  setActiveConversationId(id: string | null): void;

  // Persisted so the selected model carries from the new-chat view into the
  // canonical conversation URL without a reset.
  selectedModelId: string;
  setSelectedModelId(id: string): void;

  // Sidebar open/closed lives here so the chat layout (which renders the
  // sidebar) and the chat pages (which render the title header + the fixed
  // re-open toggle) stay in sync without prop-drilling through the page tree.
  isSidebarOpen: boolean;
  toggleSidebar(): void;

  // Client-side pinning. The backend has no pinned flag, so the set of pinned
  // conversation ids is persisted to localStorage and merged into the sidebar
  // list presentationally.
  pinnedIds: Set<string>;
  togglePin(id: string): void;
  isPinned(id: string): boolean;
}

const PINNED_STORAGE_KEY = "trinity:pinnedConversations";
const MOBILE_BREAKPOINT = 768;

function readPinned(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.localStorage.getItem(PINNED_STORAGE_KEY);
    const parsed = raw ? (JSON.parse(raw) as unknown) : [];
    return new Set(
      Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === "string") : [],
    );
  } catch {
    return new Set();
  }
}

const ChatSessionContext = createContext<ChatSessionValue | null>(null);

export function ChatSessionProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const convs = useConversations();
  // On every completed message, reconcile both the balance and the sidebar
  // list with the backend. Refreshing conversations here means a brand-new
  // chat surfaces in the sidebar regardless of which model was selected (BUG 8).
  // The balance event refreshes EVERY consumer (sidebar useAuth + any open
  // balance page) so the debit shows instantly, not only on reload (tr-hye6).
  const handleMessageDone = useCallback(() => {
    dispatchBalanceRefresh();
    convs.refresh();
  }, [convs.refresh]);
  const chat = useChat(handleMessageDone);
  const [activeConversationId, setActiveConversationId] = useState<
    string | null
  >(null);
  const [selectedModelId, setSelectedModelId] = useState<string>("");

  const [isSidebarOpen, setIsSidebarOpen] = useState(true);
  const [pinnedIds, setPinnedIds] = useState<Set<string>>(() => new Set());

  // Hydrate pinned ids + initial sidebar state on the client only (avoids an
  // SSR/client mismatch — localStorage and innerWidth aren't available on the
  // server).
  useEffect(() => {
    setPinnedIds(readPinned());
    setIsSidebarOpen(window.innerWidth >= MOBILE_BREAKPOINT);
  }, []);

  const toggleSidebar = useCallback(() => setIsSidebarOpen((v) => !v), []);

  const togglePin = useCallback((id: string) => {
    setPinnedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      try {
        window.localStorage.setItem(
          PINNED_STORAGE_KEY,
          JSON.stringify([...next]),
        );
      } catch {
        /* storage unavailable — keep in-memory state only */
      }
      return next;
    });
  }, []);

  const isPinned = useCallback((id: string) => pinnedIds.has(id), [pinnedIds]);

  const value: ChatSessionValue = {
    messages: chat.messages,
    isStreaming: chat.isStreaming,
    error: chat.error,
    webSearchEnabled: chat.webSearchEnabled,
    toggleWebSearch: chat.toggleWebSearch,
    thinkingEnabled: chat.thinkingEnabled,
    thinkingEffort: chat.thinkingEffort,
    toggleThinking: chat.toggleThinking,
    toolsEnabled: chat.toolsEnabled,
    toggleTools: chat.toggleTools,
    applyConversationSettings: chat.applyConversationSettings,
    setThinkingEffort: chat.setThinkingEffort,
    sendMessage: chat.sendMessage,
    stop: chat.stop,
    clearError: chat.clearError,
    setInitialMessages: chat.setInitialMessages,

    setActiveConversation: chat.setActiveConversation,
    regenerate: chat.regenerate,
    editAndResend: chat.editAndResend,
    switchVersion: chat.switchVersion,
    conversationVersions: chat.conversationVersions,
    loadConversationVersions: chat.loadConversationVersions,

    conversationImages: chat.conversationImages,
    loadConversationImages: chat.loadConversationImages,
    clearConversationImages: chat.clearConversationImages,
    toggleImageContext: chat.toggleImageContext,

    conversationFiles: chat.conversationFiles,
    loadConversationFiles: chat.loadConversationFiles,
    clearConversationFiles: chat.clearConversationFiles,
    toggleFileContext: chat.toggleFileContext,

    conversations: convs.conversations,
    isLoadingConversations: convs.isLoading,
    createConversation: convs.createConversation,
    deleteConversation: convs.deleteConversation,
    renameConversation: convs.renameConversation,
    refreshConversations: convs.refresh,
    loadAllConversations: convs.loadAll,
    isLoadingAllConversations: convs.isLoadingAll,
    allConversationsLoaded: convs.loadedAll,

    archivedConversations: convs.archived,
    isLoadingArchived: convs.isLoadingArchived,
    loadArchived: convs.loadArchived,
    restoreConversation: convs.restoreConversation,
    permanentDeleteConversation: convs.permanentDeleteConversation,

    activeConversationId,
    setActiveConversationId,
    selectedModelId,
    setSelectedModelId,

    isSidebarOpen,
    toggleSidebar,
    pinnedIds,
    togglePin,
    isPinned,
  };

  return (
    <ChatSessionContext.Provider value={value}>
      {children}
    </ChatSessionContext.Provider>
  );
}

export function useChatSession(): ChatSessionValue {
  const ctx = useContext(ChatSessionContext);
  if (ctx === null) {
    throw new Error("useChatSession must be used within a ChatSessionProvider");
  }
  return ctx;
}
