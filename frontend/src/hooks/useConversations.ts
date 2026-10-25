"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiRequestError, apiClient } from "@/lib/api";
import type {
  ConversationListResponse,
  ConversationResponse,
} from "@/types/api";

export interface ConversationSummary {
  id: string;
  title: string | null;
  model_id: string;
  updated_at: string;
}

export interface UseConversationsReturn {
  conversations: ConversationSummary[];
  isLoading: boolean;
  createConversation(modelId: string): Promise<string>;
  deleteConversation(id: string): Promise<void>;
  renameConversation(id: string, title: string): Promise<void>;
  refresh(): void;

  // Full list, loaded lazily when the «Чаты» overlay opens (View all).
  loadAll(): Promise<void>;
  isLoadingAll: boolean;
  loadedAll: boolean;

  // Archive (soft-deleted) conversations — loaded lazily when the archive
  // section is expanded (FEATURE 3).
  archived: ConversationSummary[];
  isLoadingArchived: boolean;
  loadArchived(): Promise<void>;
  restoreConversation(id: string): Promise<void>;
  permanentDeleteConversation(id: string): Promise<void>;
}

function toSummary(c: ConversationResponse): ConversationSummary {
  return {
    id: c.id,
    title: c.title,
    model_id: c.model_id,
    updated_at: c.updated_at,
  };
}

export function useConversations(): UseConversationsReturn {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [archived, setArchived] = useState<ConversationSummary[]>([]);
  const [isLoadingArchived, setIsLoadingArchived] = useState(false);
  const [isLoadingAll, setIsLoadingAll] = useState(false);
  const [loadedAll, setLoadedAll] = useState(false);
  const loadingAllRef = useRef(false);
  const cancelledRef = useRef(false);

  const load = useCallback(async () => {
    setIsLoading(true);
    try {
      const res = await apiClient.get<ConversationListResponse>(
        "/api/v1/chat/conversations?page=1&per_page=50",
      );
      if (!cancelledRef.current) {
        setConversations(res.items.map(toSummary));
      }
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) return;
      // Transient: keep prior list.
    } finally {
      if (!cancelledRef.current) setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    cancelledRef.current = false;
    void load();
    return () => {
      cancelledRef.current = true;
    };
  }, [load]);

  const loadAll = useCallback(async () => {
    if (loadingAllRef.current) return; // guard against concurrent fan-out
    loadingAllRef.current = true;
    setIsLoadingAll(true);
    try {
      const perPage = 100;
      let pageNo = 1;
      let collected: ConversationSummary[] = [];
      let total = Number.POSITIVE_INFINITY;
      while (collected.length < total) {
        const res = await apiClient.get<ConversationListResponse>(
          `/api/v1/chat/conversations?page=${pageNo}&per_page=${perPage}`,
        );
        total = res.total;
        collected = collected.concat(res.items.map(toSummary));
        if (res.items.length === 0) break;
        pageNo += 1;
      }
      if (!cancelledRef.current) {
        setConversations(collected);
        setLoadedAll(true);
      }
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) return;
      // Transient: keep prior list.
    } finally {
      loadingAllRef.current = false;
      if (!cancelledRef.current) setIsLoadingAll(false);
    }
  }, []);

  const createConversation = useCallback(
    async (modelId: string): Promise<string> => {
      const conv = await apiClient.post<ConversationResponse>(
        "/api/v1/chat/conversations",
        { model_id: modelId },
      );
      setConversations((prev) => [toSummary(conv), ...prev]);
      return conv.id;
    },
    [],
  );

  const deleteConversation = useCallback(async (id: string) => {
    const prev = conversations;
    setConversations((cur) => cur.filter((c) => c.id !== id));
    try {
      await apiClient.delete(`/api/v1/chat/conversations/${id}`);
    } catch {
      setConversations(prev);
      throw new Error("Не удалось удалить диалог.");
    }
  }, [conversations]);

  const renameConversation = useCallback(
    async (id: string, title: string) => {
      const trimmed = title.trim();
      if (trimmed.length === 0) return;
      const prev = conversations;
      setConversations((cur) =>
        cur.map((c) => (c.id === id ? { ...c, title: trimmed } : c)),
      );
      try {
        await apiClient.patch<ConversationResponse>(
          `/api/v1/chat/conversations/${id}`,
          { title: trimmed },
        );
      } catch {
        setConversations(prev);
        throw new Error("Не удалось переименовать диалог.");
      }
    },
    [conversations],
  );

  const loadArchived = useCallback(async () => {
    setIsLoadingArchived(true);
    try {
      const res = await apiClient.get<ConversationListResponse>(
        "/api/v1/chat/conversations?archived=true&page=1&per_page=50",
      );
      if (!cancelledRef.current) setArchived(res.items.map(toSummary));
    } catch {
      // Transient: keep prior list.
    } finally {
      if (!cancelledRef.current) setIsLoadingArchived(false);
    }
  }, []);

  const restoreConversation = useCallback(
    async (id: string) => {
      await apiClient.post<ConversationResponse>(
        `/api/v1/chat/conversations/${id}/restore`,
      );
      setArchived((cur) => cur.filter((c) => c.id !== id));
      if (loadedAll) await loadAll();
      else await load();
    },
    [load, loadAll, loadedAll],
  );

  const permanentDeleteConversation = useCallback(async (id: string) => {
    const prev = archived;
    setArchived((cur) => cur.filter((c) => c.id !== id));
    try {
      await apiClient.delete(`/api/v1/chat/conversations/${id}/permanent`);
    } catch {
      setArchived(prev);
      throw new Error("Не удалось удалить диалог навсегда.");
    }
  }, [archived]);

  return {
    conversations,
    isLoading,
    createConversation,
    deleteConversation,
    renameConversation,
    refresh: load,
    loadAll,
    isLoadingAll,
    loadedAll,
    archived,
    isLoadingArchived,
    loadArchived,
    restoreConversation,
    permanentDeleteConversation,
  };
}
