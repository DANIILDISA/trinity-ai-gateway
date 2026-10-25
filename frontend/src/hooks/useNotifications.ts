"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiRequestError, apiClient } from "@/lib/api";
import { refreshIfNeeded } from "@/lib/auth";
import type {
  Notification,
  NotificationListResponse,
} from "@/types/api";

const POLL_INTERVAL_MS = 60_000;

export interface UseNotificationsReturn {
  notifications: Notification[];
  unreadCount: number;
  isLoading: boolean;
  markAsRead(id: string): Promise<void>;
  refresh(): void;
}

export function useNotifications(): UseNotificationsReturn {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const cancelledRef = useRef(false);

  const load = useCallback(async () => {
    try {
      const res = await apiClient.get<NotificationListResponse>(
        "/api/v1/users/me/notifications?page=1&per_page=50",
      );
      if (!cancelledRef.current) setNotifications(res.items ?? []);
    } catch (err) {
      if (err instanceof ApiRequestError) {
        if (err.status === 401) return;
        if (err.status === 404) {
          // Backend hasn't implemented the endpoint yet — degrade silently.
          if (!cancelledRef.current) setNotifications([]);
        }
      }
    } finally {
      if (!cancelledRef.current) setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    cancelledRef.current = false;
    void load();
    const id = setInterval(() => {
      // Skip the tick while the tab is hidden: the background-throttled refresh
      // timer may have let the token expire, so polling here would fire a stale
      // token and 401 (tr-wd8). The visibilitychange handler resumes it.
      if (document.hidden) return;
      void load();
    }, POLL_INTERVAL_MS);

    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      // Regaining focus: refresh a token the throttled timer let go stale BEFORE
      // the catch-up poll, then fetch — so the resumed poll never 401s.
      void refreshIfNeeded().then(() => {
        if (!cancelledRef.current) void load();
      });
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      cancelledRef.current = true;
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load]);

  const markAsRead = useCallback(async (id: string) => {
    setNotifications((prev) =>
      prev.map((n) => (n.id === id ? { ...n, is_read: true } : n)),
    );
    try {
      await apiClient.post(`/api/v1/users/me/notifications/${id}/read`);
    } catch {
      // Best-effort: leave optimistic state. Next poll will reconcile.
    }
  }, []);

  const unreadCount = notifications.reduce(
    (acc, n) => acc + (n.is_read ? 0 : 1),
    0,
  );

  return {
    notifications,
    unreadCount,
    isLoading,
    markAsRead,
    refresh: load,
  };
}
