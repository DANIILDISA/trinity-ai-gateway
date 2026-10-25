"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiRequestError, apiClient } from "@/lib/api";
import { refreshIfNeeded } from "@/lib/auth";
import { onBalanceRefresh } from "@/lib/balanceEvents";
import { formatBalance } from "@/lib/formatting";
import type { User } from "@/types/api";

const POLL_INTERVAL_MS = 30_000;

export interface UseBalanceReturn {
  balance: string | null;
  balanceRaw: string | null;
  isLoading: boolean;
  refresh: () => void;
}

export function useBalance(): UseBalanceReturn {
  const [balanceRaw, setBalanceRaw] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const cancelledRef = useRef(false);

  const load = useCallback(async () => {
    try {
      const me = await apiClient.get<User>("/api/v1/users/me");
      // BUG 8: only overwrite when the response actually carries a balance.
      // A missing/empty field must never blank out a previously shown value.
      if (
        !cancelledRef.current &&
        me.balance != null &&
        me.balance !== ""
      ) {
        setBalanceRaw(me.balance);
      }
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) {
        // Auth guard / useAuth handles redirect; don't retry.
        return;
      }
      // Transient errors: leave previous value visible.
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

    // tr-hye6: refetch the instant a balance change is signalled (top-up landed,
    // chat-send debit, admin adjustment) — no 30s wait, no reload.
    const unsubscribe = onBalanceRefresh(() => void load());

    return () => {
      cancelledRef.current = true;
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
      unsubscribe();
    };
  }, [load]);

  const refresh = useCallback(() => {
    void load();
  }, [load]);

  return {
    balance: balanceRaw ? formatBalance(balanceRaw) : null,
    balanceRaw,
    isLoading,
    refresh,
  };
}
