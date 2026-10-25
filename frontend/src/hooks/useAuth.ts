"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { ApiRequestError, apiClient } from "@/lib/api";
import { getErrorMessage } from "@/lib/errors";
import {
  clearAuth,
  getAccessToken,
  refreshAccessToken,
  scheduleTokenRefresh,
  setAccessToken,
  subscribeAuth,
} from "@/lib/auth";
import { onBalanceRefresh } from "@/lib/balanceEvents";
import type { AuthResponse, User } from "@/types/api";

export interface UseAuthReturn {
  user: User | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  /** True when hydration could not reach the backend (network / 5xx) so the
   *  auth status is unknown — distinct from a confirmed logout. Callers must NOT
   *  treat this as unauthenticated: the session is likely still valid (tr-lgm). */
  connectionError: boolean;
  /** Re-run hydration — used by the reconnect screen to recover once the backend
   *  is reachable again. */
  retry(): void;
  login(email: string, password: string): Promise<void>;
  register(
    email: string,
    password: string,
    displayName: string,
  ): Promise<void>;
  recover(email: string, password: string): Promise<void>;
  logout(): Promise<void>;
  refreshUser(): Promise<void>;
}

/** Result of resolving the current session during hydration. */
type HydrateStatus = "authenticated" | "unauthenticated" | "unavailable";

export function useAuth(): UseAuthReturn {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [connectionError, setConnectionError] = useState(false);
  const mountedRef = useRef(true);

  // Fetch /users/me, distinguishing a 401 (clear auth) from a transient failure
  // (keep the token — the session is fine, the backend is just unreachable).
  const loadMe = useCallback(async (): Promise<HydrateStatus> => {
    try {
      const me = await apiClient.get<User>("/api/v1/users/me");
      if (mountedRef.current) setUser(me);
      return "authenticated";
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) {
        if (mountedRef.current) clearAuth();
        return "unauthenticated";
      }
      return "unavailable";
    }
  }, []);

  const hydrate = useCallback(async (): Promise<void> => {
    // connectionError is left sticky across a retry (only updated once the
    // attempt resolves below) so the reconnect screen doesn't flicker to a bare
    // spinner on every auto-retry tick.
    if (mountedRef.current) setIsLoading(true);

    let status: HydrateStatus;
    if (getAccessToken()) {
      status = await loadMe();
    } else {
      const outcome = await refreshAccessToken();
      if (outcome === "refreshed") status = await loadMe();
      else if (outcome === "unauthenticated") status = "unauthenticated";
      else status = "unavailable";
    }

    if (!mountedRef.current) return;
    // Only a transient failure sets connectionError; a confirmed
    // "unauthenticated" leaves user=null so the layout routes to /login.
    setConnectionError(status === "unavailable");
    setIsLoading(false);
  }, [loadMe]);

  useEffect(() => {
    mountedRef.current = true;
    void hydrate();

    const unsubscribe = subscribeAuth((token) => {
      if (token === null) setUser(null);
    });
    // tr-hye6: refetch the user — and with it the sidebar balance/reserved/
    // available — the instant a balance change is signalled (top-up landed,
    // chat-send debit), so the sidebar updates without a reload.
    const unsubscribeBalance = onBalanceRefresh(() => {
      if (getAccessToken()) void loadMe();
    });

    return () => {
      mountedRef.current = false;
      unsubscribe();
      unsubscribeBalance();
    };
  }, [hydrate, loadMe]);

  const retry = useCallback(() => {
    void hydrate();
  }, [hydrate]);

  const login = useCallback(
    async (email: string, password: string) => {
      try {
        const res = await apiClient.post<AuthResponse>("/api/v1/auth/login", {
          email,
          password,
        });
        setAccessToken(res.access_token);
        scheduleTokenRefresh();
        setUser(res.user as User);
        router.push("/chat");
      } catch (err) {
        throw translateAuthError(err, "Не удалось войти. Попробуйте ещё раз.");
      }
    },
    [router],
  );

  const register = useCallback(
    async (email: string, password: string, displayName: string) => {
      try {
        const res = await apiClient.post<AuthResponse>(
          "/api/v1/auth/register",
          { email, password, display_name: displayName },
        );
        setAccessToken(res.access_token);
        scheduleTokenRefresh();
        setUser(res.user as User);
        router.push("/chat");
      } catch (err) {
        throw translateAuthError(
          err,
          "Не удалось зарегистрироваться. Попробуйте ещё раз.",
        );
      }
    },
    [router],
  );

  const recover = useCallback(
    async (email: string, password: string) => {
      try {
        const res = await apiClient.post<AuthResponse>("/api/v1/auth/recover", {
          email,
          password,
        });
        setAccessToken(res.access_token);
        scheduleTokenRefresh();
        setUser(res.user as User);
        router.push("/chat");
      } catch (err) {
        throw translateAuthError(
          err,
          "Не удалось восстановить аккаунт. Попробуйте ещё раз.",
        );
      }
    },
    [router],
  );

  const refreshUser = useCallback(async () => {
    try {
      const me = await apiClient.get<User>("/api/v1/users/me");
      setUser(me);
    } catch {
      /* leave existing user */
    }
  }, []);

  const logout = useCallback(async () => {
    try {
      await apiClient.post("/api/v1/auth/logout");
    } catch {
      // ignore — clear local state regardless
    }
    clearAuth();
    setUser(null);
    router.push("/login");
  }, [router]);

  return {
    user,
    isLoading,
    isAuthenticated: user !== null,
    connectionError,
    retry,
    login,
    register,
    recover,
    logout,
    refreshUser,
  };
}

function translateAuthError(err: unknown, fallback: string): Error {
  if (err instanceof ApiRequestError) {
    // Prefer the §9.2 code mapping (e.g. email_already_registered →
    // "Пользователь с таким email уже существует"), falling back to the
    // server-provided message, then the generic fallback.
    return new Error(getErrorMessage(err.code, err.message || fallback));
  }
  return new Error(fallback);
}
