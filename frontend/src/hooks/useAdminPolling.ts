"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiRequestError } from "@/lib/api";

const DEFAULT_INTERVAL_MS = 30_000;

export interface UseAdminPollingReturn<T> {
  data: T | null;
  isLoading: boolean;
  error: string | null;
  refresh: () => void;
}

export function useAdminPolling<T>(
  fetchFn: () => Promise<T>,
  intervalMs: number = DEFAULT_INTERVAL_MS,
): UseAdminPollingReturn<T> {
  const [data, setData] = useState<T | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const cancelledRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const fetchRef = useRef(fetchFn);
  fetchRef.current = fetchFn;

  const tick = useCallback(async () => {
    try {
      const result = await fetchRef.current();
      if (cancelledRef.current) return;
      setData(result);
      setError(null);
    } catch (err) {
      if (cancelledRef.current) return;
      const message =
        err instanceof ApiRequestError
          ? err.message
          : "Не удалось загрузить данные.";
      setError(message);
      // Keep stale data — do not setData(null).
    } finally {
      if (!cancelledRef.current) setIsLoading(false);
    }
  }, []);

  const arm = useCallback(() => {
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = setInterval(() => {
      void tick();
    }, intervalMs);
  }, [intervalMs, tick]);

  useEffect(() => {
    cancelledRef.current = false;
    void tick();
    arm();
    return () => {
      cancelledRef.current = true;
      if (timerRef.current) clearInterval(timerRef.current);
      timerRef.current = null;
    };
  }, [tick, arm]);

  const refresh = useCallback(() => {
    setIsLoading(true);
    void tick();
    arm();
  }, [tick, arm]);

  return { data, isLoading, error, refresh };
}
