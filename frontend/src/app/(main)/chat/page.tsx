"use client";

import { useCallback, useEffect, useState } from "react";

import { ChatWindow } from "@/components/chat/ChatWindow";
import { useChatSession } from "@/components/chat/ChatSessionProvider";
import { useModels } from "@/components/chat/ModelSelector";
import { useAuth } from "@/hooks/useAuth";
import type { AttachedFile, AttachedImage } from "@/hooks/useChat";
import { ApiRequestError } from "@/lib/api";
import { getErrorMessage } from "@/lib/errors";

export default function NewChatPage() {
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
    createConversation,
    activeConversationId,
    setActiveConversationId,
    selectedModelId,
    setSelectedModelId,
    conversationImages,
    toggleImageContext,
    conversationFiles,
    toggleFileContext,
    conversationVersions,
    loadConversationVersions,
    switchVersion,
    regenerate,
    editAndResend,
    isSidebarOpen,
  } = useChatSession();

  const [inputValue, setInputValue] = useState("");
  // Conversation-create failure surfaced through the normal in-chat error UI
  // (not a blocking window.alert). Kept separate from the stream error so
  // clearing one can't mask the other (tr-w4fy.2).
  const [createError, setCreateError] = useState<string | null>(null);

  // Branching actions are wired here too: after the first send this page stays
  // mounted (the URL is swapped via replaceState, no navigation), so the just-
  // streamed turns must support regenerate / edit / resend / switch without a
  // reload. activeConversationId is the conversation the provider targets.
  const handleLoadVersions = useCallback(() => {
    if (activeConversationId) void loadConversationVersions(activeConversationId);
  }, [activeConversationId, loadConversationVersions]);

  const handleSwitchVersion = useCallback(
    (nodeId: string) => {
      void switchVersion(nodeId);
    },
    [switchVersion],
  );

  // tr-77u: regenerate/edit/resend must use the model CURRENTLY selected in the
  // picker (injected as the default), not the conversation's stored model which
  // only updates on a normal send. An explicit opts.modelId still wins.
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

  // Initialize selected model once the list arrives (only when not already set
  // — the provider keeps the choice across the new→existing transition).
  useEffect(() => {
    if (selectedModelId) return;
    const firstAvailable =
      models.find((m) => m.status === "available") ?? models[0];
    if (firstAvailable) setSelectedModelId(firstAvailable.id);
  }, [models, selectedModelId, setSelectedModelId]);

  const handleSend = useCallback(
    async (images: AttachedImage[], files: AttachedFile[]) => {
      const content = inputValue.trim();
      if (
        (!content && images.length === 0 && files.length === 0) ||
        !selectedModelId
      )
        return;
      setCreateError(null);

      // Reuse the conversation once it exists so a second message in the same
      // session doesn't spawn a duplicate conversation.
      let convId = activeConversationId;
      if (!convId) {
        try {
          convId = await createConversation(selectedModelId);
        } catch (err) {
          // Keep the typed message in the composer — the user retries with one
          // click instead of retyping (tr-w4fy.2).
          setCreateError(
            err instanceof ApiRequestError
              ? getErrorMessage(err.code, err.message)
              : "Не удалось создать диалог. Попробуйте ещё раз.",
          );
          return;
        }
        setActiveConversationId(convId);
        // Reflect the canonical URL WITHOUT a Next.js navigation, so this page
        // stays mounted and the SSE stream keeps rendering into shared state.
        window.history.replaceState(null, "", `/chat/${convId}`);
      }

      setInputValue("");
      await sendMessage(content, selectedModelId, convId, images, files);
    },
    [
      inputValue,
      selectedModelId,
      activeConversationId,
      createConversation,
      setActiveConversationId,
      sendMessage,
    ],
  );

  const onSend = useCallback(
    (images: AttachedImage[], files: AttachedFile[]) => {
      void handleSend(images, files);
    },
    [handleSend],
  );

  return (
    <ChatWindow
      messages={messages}
      isStreaming={isStreaming}
      error={createError ?? error}
      models={models}
      selectedModelId={selectedModelId}
      inputValue={inputValue}
      isLoadingHistory={false}
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
      sidebarOpen={isSidebarOpen}
      conversationId={activeConversationId ?? undefined}
      conversationTitle={null}
      conversationMenu={null}
      userName={user?.display_name ?? null}
      onToggleImageContext={toggleImageContext}
      onToggleFileContext={toggleFileContext}
      onModelChange={setSelectedModelId}
      onInputChange={setInputValue}
      onSend={onSend}
      onStop={stop}
      onClearError={() => {
        setCreateError(null);
        clearError();
      }}
      onToggleWebSearch={toggleWebSearch}
      onToggleThinking={toggleThinking}
      onSetThinkingEffort={setThinkingEffort}
      onToggleTools={toggleTools}
    />
  );
}
