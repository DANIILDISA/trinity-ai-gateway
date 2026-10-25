import { ApiRequestError, apiClient } from "@/lib/api";
import type { AuthResponse } from "@/types/api";

const ACCESS_TOKEN_LIFETIME_MS = 15 * 60 * 1000;
const REFRESH_FRACTION = 0.9;
// After a transient refresh failure (network / 5xx), keep trying in the
// background so the session recovers on its own once the backend is back, rather
// than stranding the user. Short enough to feel responsive, long enough not to
// hammer a struggling backend.
const TRANSIENT_RETRY_MS = 5000;
// Treat the token as "stale" once it's within this window of expiry. Refocus
// refreshes a little early so the very next poll never races an expiring token
// (tr-wd8). Mirrors REFRESH_FRACTION: anything past 90% of its life is renewed.
const NEAR_EXPIRY_MS = ACCESS_TOKEN_LIFETIME_MS * (1 - REFRESH_FRACTION);

/** Outcome of a refresh attempt — lets callers distinguish a genuine logout
 *  (401) from a recoverable backend blip (network / 5xx), so a transient outage
 *  is never mistaken for "not authenticated" (tr-lgm). */
export type RefreshOutcome = "refreshed" | "unauthenticated" | "unavailable";

let accessToken: string | null = null;
// Wall-clock instant the current access token is expected to expire. Tracked so
// a refocused tab can tell whether the throttled refresh timer fell behind and
// the token is already stale, and refresh BEFORE issuing the next request
// instead of letting it 401 (tr-wd8). null = unknown (no live token).
let accessTokenExpiresAt: number | null = null;
let refreshTimer: ReturnType<typeof setTimeout> | null = null;
// Collapses concurrent refresh attempts (several useAuth instances hydrate in
// parallel on a fresh page load) onto a single /auth/refresh round-trip.
let inflightRefresh: Promise<RefreshOutcome> | null = null;

type Listener = (token: string | null) => void;
const listeners = new Set<Listener>();

export function getAccessToken(): string | null {
  return accessToken;
}

export function setAccessToken(token: string | null): void {
  accessToken = token;
  apiClient.setToken(token);
  for (const fn of listeners) fn(token);
}

export function subscribeAuth(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function scheduleTokenRefresh(
  expiresInMs: number = ACCESS_TOKEN_LIFETIME_MS,
): void {
  if (refreshTimer) clearTimeout(refreshTimer);
  accessTokenExpiresAt = Date.now() + expiresInMs;
  const delay = Math.max(1000, Math.floor(expiresInMs * REFRESH_FRACTION));
  refreshTimer = setTimeout(() => {
    void refreshAccessToken();
  }, delay);
}

/** True when there's no live token or it's within NEAR_EXPIRY_MS of expiry. */
function isAccessTokenStale(): boolean {
  if (!accessToken) return false;
  if (accessTokenExpiresAt == null) return false;
  return Date.now() >= accessTokenExpiresAt - NEAR_EXPIRY_MS;
}

/**
 * Refresh the access token only if it's stale (near/past expiry), otherwise a
 * no-op. Reuses the inflight-deduped refreshAccessToken so it never double-fires
 * alongside the retry interceptor or a concurrent caller. Pollers call this on
 * refocus to avoid sending a request with a token the throttled background timer
 * let expire (tr-wd8).
 */
export async function refreshIfNeeded(): Promise<RefreshOutcome | "fresh"> {
  if (inflightRefresh) return inflightRefresh;
  if (!isAccessTokenStale()) return "fresh";
  return refreshAccessToken();
}

export function refreshAccessToken(): Promise<RefreshOutcome> {
  if (inflightRefresh) return inflightRefresh;
  inflightRefresh = doRefresh().finally(() => {
    inflightRefresh = null;
  });
  return inflightRefresh;
}

async function doRefresh(): Promise<RefreshOutcome> {
  try {
    const res = await apiClient.post<AuthResponse>("/api/v1/auth/refresh");
    setAccessToken(res.access_token);
    scheduleTokenRefresh();
    return "refreshed";
  } catch (err) {
    if (err instanceof ApiRequestError && err.status === 401) {
      // Genuinely unauthenticated: the refresh cookie is missing, expired, or
      // revoked. Clear local state and let callers route to /login.
      clearAuth();
      return "unauthenticated";
    }
    // Transient (network error / 5xx / overloaded). The session is almost
    // certainly still valid — the persistent refresh cookie was never cleared —
    // so DO NOT clear auth: doing so turns a backend blip (e.g. a Stop-induced
    // 503 storm) into a spurious logout (tr-lgm). Keep the refresh chain alive so
    // the session recovers automatically once the backend is reachable again.
    scheduleTransientRetry();
    return "unavailable";
  }
}

function scheduleTransientRetry(): void {
  if (refreshTimer) clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    void refreshAccessToken();
  }, TRANSIENT_RETRY_MS);
}

// Wire the ApiClient's 401 recovery to the refresh flow (DI, to avoid an
// api↔auth import cycle). On a 401 from a protected call the client refreshes
// once and retries; reusing refreshAccessToken() collapses concurrent 401s onto
// the single inflight /auth/refresh. This is what makes an expired access token
// self-heal on the very next request instead of waiting for the background
// refresh timer — which browsers throttle/freeze in backgrounded tabs, the
// source of the un-recovered 401 bursts in tr-qrx.
apiClient.setUnauthorizedHandler(async () => {
  const outcome = await refreshAccessToken();
  return outcome === "refreshed";
});

export function clearAuth(): void {
  if (refreshTimer) {
    clearTimeout(refreshTimer);
    refreshTimer = null;
  }
  accessTokenExpiresAt = null;
  setAccessToken(null);
}

// A backgrounded tab has its refresh setTimeout throttled/frozen, so the access
// token can silently expire. When the tab is refocused, proactively refresh if
// the timer fell behind — BEFORE the resumed pollers fire — so they never send a
// stale token and trigger a 401 burst (tr-wd8). refreshIfNeeded is a no-op when
// the token is still fresh and is deduped against any inflight refresh, so this
// never double-refreshes alongside the retry interceptor or the poll hooks.
if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") void refreshIfNeeded();
  });
}
