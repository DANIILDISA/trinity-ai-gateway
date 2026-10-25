"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";

import { ChatWindow } from "@/components/chat/ChatWindow";
import { useChatSession } from "@/components/chat/ChatSessionProvider";
import { useModels } from "@/components/chat/ModelSelector";
import { useAuth } from "@/hooks/useAuth";
import {
  mapResponseToMessage,
  type AttachedFile,
  type AttachedImage,
  type ChatMessage,
} from "@/hooks/useChat";
import { ApiRequestError, apiClient } from "@/lib/api";
import type { BranchTotals, ConversationResponse } from "@/types/api";

export default function ExistingChatPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const conversationId = params?.id ?? "";

  const { models } = useModels();
  const { user } = useAuth();
  const {
    messages,
    isStreaming,
    error,
    sendMessage,
    stop,
    clearError,
    webSearchEnabled,
    toggleWebSearch,
    thinkingEnabled,
    thinkingEffort,
    toggleThinking,
    setThinkingEffort,
    toolsEnabled,
    toggleTools,
    applyConversationSettings,
    setInitialMessages,
    setActiveConversation,
    activeConversationId,
    setActiveConversationId,
    selectedModelId,
    setSelectedModelId,
    conversationImages,
    loadConversationImages,
    toggleImageContext,
    conversationFiles,
    loadConversationFiles,
    toggleFileContext,
    conversationVersions,
    loadConversationVersions,
    switchVersion,
    regenerate,
    editAndResend,
    isSidebarOpen,
    conversations,
    deleteConversation,
    renameConversation,
    pinnedIds,
    togglePin,
  } = useChatSession();

  const conversation = conversations.find((c) => c.id === conversationId);
  const conversationTitle = conversation?.title ?? null;

  const conversationMenu = useMemo(
    () =>
      conversationId
        ? {
            isPinned: pinnedIds.has(conversationId),
            onTogglePin: () => togglePin(conversationId),
            onRename: (title: string) => {
              void renameConversation(conversationId, title).catch((err) => {
                window.alert(
                  err instanceof Error ? err.message : "Ошибка переименования.",
                );
              });
            },
            onDelete: () => {
              void deleteConversation(conversationId)
                .then(() => {
                  setInitialMessages([]);
                  setActiveConversationId(null);
                  router.push("/chat");
                })
                .catch((err: unknown) => {
                  window.alert(
                    err instanceof Error ? err.message : "Ошибка удаления.",
                  );
                });
            },
          }
        : null,
    [
      conversationId,
      pinnedIds,
      togglePin,
      renameConversation,
      deleteConversation,
      setInitialMessages,
      setActiveConversationId,
      router,
    ],
  );

  const [inputValue, setInputValue] = useState("");
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  // All-branch aggregate from the loaded conversation (Task 13), forwarded to
  // ChatWindow → MicroAnalytics for the «Все ветки» section.
  const [branchTotals, setBranchTotals] = useState<BranchTotals | undefined>(
    undefined,
  );

  // Read activeConversationId via a ref so it is NOT an effect dependency.
  // Otherwise clicking "Новый чат" (which sets activeConversationId=null while
  // this page is still mounted) re-fires the effect and reloads the current
  // conversation instead of opening a fresh one (BUG 2). The effect should only
  // run on mount / route (conversationId) change.
  const activeConvRef = useRef(activeConversationId);
  useEffect(() => {
    activeConvRef.current = activeConversationId;
  }, [activeConversationId]);

  // Load history only when the provider isn't already showing this
  // conversation. When we arrived here from the new-chat flow, the provider
  // already holds the streamed messages — refetching would clobber them (and
  // race the still-saving assistant message), which was the root of BUG 1.
  useEffect(() => {
    if (!conversationId) return;
    // Point the hook's branching actions (regenerate/edit/switch) at this
    // conversation, including the new-chat path below where the history fetch is
    // skipped — otherwise reload-then-regenerate has no conversation id.
    setActiveConversation(conversationId);
    if (activeConvRef.current === conversationId) return;

    let cancelled = false;
    async function load() {
      setIsLoadingHistory(true);
      setHistoryError(null);
      try {
        const conv = await apiClient.get<ConversationResponse>(
          `/api/v1/chat/conversations/${conversationId}`,
        );
        if (cancelled) return;
        setSelectedModelId(conv.model_id);
        // Restore the generation posture of the last request in this chat (model
        // above + toggles here) so reopening it matches how it was last used
        // (tr-hsg.12). null fields are skipped → defaults kept.
        applyConversationSettings({
          thinkingEnabled: conv.last_thinking_enabled,
          thinkingEffort: conv.last_thinking_effort,
          toolsEnabled: conv.last_tools_enabled,
          webSearchEnabled: conv.last_web_search_enabled,
        });
        setBranchTotals(conv.branch_totals);
        // Use the canonical mapper (useChat) so reload restores the SAME shape a
        // live stream produces — incl. thinking_segments AND tool_steps, hence the
        // tool timeline + artifact card (tr-ygx: the old local mapMessage dropped
        // both, so reloading a tool conversation lost its steps + «Скачать» card).
        const mapped: ChatMessage[] = (conv.messages ?? [])
          .filter((m) => m.role === "user" || m.role === "assistant")
          .map(mapResponseToMessage);
        setInitialMessages(mapped);
        setActiveConversationId(conversationId);
        // Load persisted images + files so per-attachment context checkboxes
        // + analytics light up; message ids here are the DB ids they reference.
        void loadConversationImages(conversationId);
        void loadConversationFiles(conversationId);
      } catch (err) {
        if (cancelled) return;
        const msg =
          err instanceof ApiRequestError
            ? err.message
            : "Не удалось загрузить диалог.";
        setHistoryError(msg);
      } finally {
        if (!cancelled) setIsLoadingHistory(false);
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, [
    conversationId,
    setInitialMessages,
    setActiveConversation,
    setActiveConversationId,
    setSelectedModelId,
    applyConversationSettings,
    loadConversationImages,
    loadConversationFiles,
  ]);

  // Refresh the «Все ветки» (all-branch) aggregate when a generation finishes
  // (streaming true → false). branchTotals is loaded once with the conversation,
  // but send/regenerate/edit each add a new version whose spend/tokens belong in
  // the all-branch totals — without this the section stays stale until a reload
  // (spec §6A: refresh "when a generation completes"). A version switch is free
  // and adds no messages, so it never sets isStreaming and correctly skips this.
  const prevStreamingRef = useRef(isStreaming);
  useEffect(() => {
    const wasStreaming = prevStreamingRef.current;
    prevStreamingRef.current = isStreaming;
    if (!wasStreaming || isStreaming || !conversationId) return;
    let cancelled = false;
    void (async () => {
      try {
        const conv = await apiClient.get<ConversationResponse>(
          `/api/v1/chat/conversations/${conversationId}`,
        );
        // Only the aggregate — never the messages, which the hook owns and is
        // streaming-authoritative for the active path.
        if (!cancelled) setBranchTotals(conv.branch_totals);
      } catch {
        // Leave the prior totals on a transient fetch failure.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isStreaming, conversationId]);

  const handleSend = useCallback(
    async (images: AttachedImage[], files: AttachedFile[]) => {
      const content = inputValue.trim();
      if (
        (!content && images.length === 0 && files.length === 0) ||
        !selectedModelId ||
        !conversationId
      )
        return;
      setInputValue("");
      await sendMessage(content, selectedModelId, conversationId, images, files);
    },
    [inputValue, selectedModelId, conversationId, sendMessage],
  );

  const handleSendClick = useCallback(
    (images: AttachedImage[], files: AttachedFile[]) => {
      void handleSend(images, files);
    },
    [handleSend],
  );

  const handleClearError = useCallback(() => {
    clearError();
    setHistoryError(null);
  }, [clearError]);

  // Branching action wiring (§6). loadVersions fills the parent-keyed sibling
  // cache so the VersionSwitcher chevrons resolve neighbor ids; the rest call
  // straight through to the useChat actions (fire-and-forget — they stream /
  // repoint shared state). Resend re-runs a user turn unchanged via
  // editAndResend with that turn's own content.
  const handleLoadVersions = useCallback(() => {
    if (conversationId) void loadConversationVersions(conversationId);
  }, [conversationId, loadConversationVersions]);

  const handleSwitchVersion = useCallback(
    (nodeId: string) => {
      void switchVersion(nodeId);
    },
    [switchVersion],
  );

  // tr-77u: regenerate/edit/resend must use the model CURRENTLY selected in the
  // picker, not the conversation's stored model (which only updates on a normal
  // send). The picker's `selectedModelId` is injected as the default model so a
  // bare control click (no explicit opts) regenerates/edits with the new model;
  // an explicit opts.modelId (future per-version picker) still wins.
  const handleRegenerate = useCallback(
    (assistantId: string, opts?: { modelId?: string }) => {
      void regenerate(assistantId, { ...opts, modelId: opts?.modelId ?? selectedModelId });
    },
    [regenerate, selectedModelId],
  );

  const handleEditAndResend = useCallback(
    (userId: string, content: string) => {
      void editAndResend(userId, content, { modelId: selectedModelId });
    },
    [editAndResend, selectedModelId],
  );

  const handleResend = useCallback(
    (userId: string) => {
      const target = messages.find((m) => m.id === userId);
      if (!target) return;
      void editAndResend(userId, target.content, { modelId: selectedModelId });
    },
    [messages, editAndResend, selectedModelId],
  );

  return (
    <ChatWindow
      messages={messages}
      isStreaming={isStreaming}
      error={error ?? historyError}
      models={models}
      selectedModelId={selectedModelId}
      inputValue={inputValue}
      isLoadingHistory={isLoadingHistory}
      webSearchEnabled={webSearchEnabled}
      thinkingEnabled={thinkingEnabled}
      thinkingEffort={thinkingEffort}
      toolsEnabled={toolsEnabled}
      conversationImages={conversationImages}
      conversationFiles={conversationFiles}
      conversationVersions={conversationVersions}
      onLoadVersions={handleLoadVersions}
      onSwitchVersion={handleSwitchVersion}
      onRegenerate={handleRegenerate}
      onEditAndResend={handleEditAndResend}
      onResend={handleResend}
      branchTotals={branchTotals}
      sidebarOpen={isSidebarOpen}
      conversationId={conversationId}
      conversationTitle={conversationTitle}
      conversationMenu={conversationMenu}
      userName={user?.display_name ?? null}
      onToggleImageContext={toggleImageContext}
      onToggleFileContext={toggleFileContext}
      onModelChange={setSelectedModelId}
      onInputChange={setInputValue}
      onSend={handleSendClick}
      onStop={stop}
      onClearError={handleClearError}
      onToggleWebSearch={toggleWebSearch}
      onToggleThinking={toggleThinking}
      onSetThinkingEffort={setThinkingEffort}
      onToggleTools={toggleTools}
    />
  );
}

