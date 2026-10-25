"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiRequestError, apiClient } from "@/lib/api";
import type {
  ConversationListResponse,
  ConversationResponse,
} from "@/types/api";
import type { ConversationSummary } from "@/hooks/useConversations";
import { HistoryPage } from "./HistoryPage";

const PAGE_SIZE = 20;

function toSummary(c: ConversationResponse): ConversationSummary {
  return {
    id: c.id,
    title: c.title,
    model_id: c.model_id,
    updated_at: c.updated_at,
  };
}

export default function HistoryRoute() {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [isLoading, setIsLoading] = useState(true);

  const hasMore = conversations.length < total;

  const loadPage = useCallback(async (pageNum: number) => {
    setIsLoading(true);
    try {
      const res = await apiClient.get<ConversationListResponse>(
        `/api/v1/chat/conversations?page=${pageNum}&per_page=${PAGE_SIZE}`,
      );
      setTotal(res.total);
      setConversations((prev) =>
        pageNum === 1
          ? res.items.map(toSummary)
          : [...prev, ...res.items.map(toSummary)],
      );
      setPage(pageNum);
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) return;
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadPage(1);
  }, [loadPage]);

  const onDelete = useCallback(async (id: string) => {
    const prev = conversations;
    setConversations((cur) => cur.filter((c) => c.id !== id));
    setTotal((t) => Math.max(0, t - 1));
    try {
      await apiClient.delete(`/api/v1/chat/conversations/${id}`);
    } catch {
      setConversations(prev);
      window.alert("Не удалось удалить диалог.");
    }
  }, [conversations]);

  const onRename = useCallback(
    async (id: string, title: string) => {
      const trimmed = title.trim();
      if (!trimmed) return;
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
        window.alert("Не удалось переименовать диалог.");
      }
    },
    [conversations],
  );

  return (
    <HistoryPage
      conversations={conversations}
      isLoading={isLoading}
      hasMore={hasMore}
      onLoadMore={() => void loadPage(page + 1)}
      onDelete={(id) => void onDelete(id)}
      onRename={(id, title) => void onRename(id, title)}
    />
  );
}
