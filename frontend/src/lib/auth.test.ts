import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiRequestError, apiClient } from "@/lib/api";
import {
  clearAuth,
  getAccessToken,
  refreshAccessToken,
  refreshIfNeeded,
  scheduleTokenRefresh,
  setAccessToken,
} from "@/lib/auth";
import type { AuthResponse } from "@/types/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    apiClient: {
      get: vi.fn(),
      post: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn(),
      setToken: vi.fn(),
      setUnauthorizedHandler: vi.fn(),
    },
  };
});

const postMock = apiClient.post as unknown as ReturnType<typeof vi.fn>;

function authResponse(token: string): AuthResponse {
  return {
    access_token: token,
    token_type: "bearer",
    user: { id: "u1" },
  } as unknown as AuthResponse;
}

beforeEach(() => {
  vi.useFakeTimers();
  postMock.mockReset();
  clearAuth();
});

afterEach(() => {
  clearAuth();
  vi.useRealTimers();
});

describe("refreshAccessToken", () => {
  it("returns 'refreshed' and stores the new token on success", async () => {
    postMock.mockResolvedValueOnce(authResponse("new-token"));
    const outcome = await refreshAccessToken();
    expect(outcome).toBe("refreshed");
    expect(getAccessToken()).toBe("new-token");
  });

  it("returns 'unauthenticated' and clears auth on 401", async () => {
    setAccessToken("stale-token");
    postMock.mockRejectedValueOnce(new ApiRequestError("нет", 401));
    const outcome = await refreshAccessToken();
    expect(outcome).toBe("unauthenticated");
    expect(getAccessToken()).toBeNull();
  });

  it("returns 'unavailable' WITHOUT clearing auth on a 503 (backend overloaded)", async () => {
    setAccessToken("live-token");
    postMock.mockRejectedValueOnce(new ApiRequestError("перегрузка", 503));
    const outcome = await refreshAccessToken();
    expect(outcome).toBe("unavailable");
    // The session must survive a transient backend blip — this is the tr-lgm bug:
    // a 503 storm during a Stop must NOT log the user out.
    expect(getAccessToken()).toBe("live-token");
  });

  it("returns 'unavailable' WITHOUT clearing auth on a network error (status 0)", async () => {
    setAccessToken("live-token");
    postMock.mockRejectedValueOnce(new ApiRequestError("сеть", 0));
    const outcome = await refreshAccessToken();
    expect(outcome).toBe("unavailable");
    expect(getAccessToken()).toBe("live-token");
  });

  it("de-dupes concurrent callers onto a single /auth/refresh request", async () => {
    postMock.mockResolvedValue(authResponse("shared-token"));
    const [a, b, c] = await Promise.all([
      refreshAccessToken(),
      refreshAccessToken(),
      refreshAccessToken(),
    ]);
    expect(a).toBe("refreshed");
    expect(b).toBe("refreshed");
    expect(c).toBe("refreshed");
    expect(postMock).toHaveBeenCalledTimes(1);
  });
});

describe("refreshIfNeeded (tr-wd8)", () => {
  const LIFETIME_MS = 15 * 60 * 1000;

  it("is a no-op for a freshly issued token", async () => {
    setAccessToken("fresh");
    scheduleTokenRefresh(LIFETIME_MS); // expires ~15m from now
    const outcome = await refreshIfNeeded();
    expect(outcome).toBe("fresh");
    expect(postMock).not.toHaveBeenCalled();
    expect(getAccessToken()).toBe("fresh");
  });

  it("refreshes when the token is near/past expiry (throttled timer fell behind)", async () => {
    setAccessToken("stale");
    // Token whose life is almost gone — simulates a backgrounded tab whose
    // refresh timer was frozen.
    scheduleTokenRefresh(1000);
    vi.advanceTimersByTime(900);
    postMock.mockResolvedValueOnce(authResponse("renewed"));

    const outcome = await refreshIfNeeded();
    expect(outcome).toBe("refreshed");
    expect(postMock).toHaveBeenCalledTimes(1);
    expect(getAccessToken()).toBe("renewed");
  });

  it("is a no-op when there is no live token", async () => {
    const outcome = await refreshIfNeeded();
    expect(outcome).toBe("fresh");
    expect(postMock).not.toHaveBeenCalled();
  });

  it("reuses an inflight refresh instead of starting a second one", async () => {
    setAccessToken("stale");
    scheduleTokenRefresh(1000);
    vi.advanceTimersByTime(900);
    postMock.mockResolvedValue(authResponse("renewed"));

    const inflight = refreshAccessToken();
    const piggyback = refreshIfNeeded();
    await Promise.all([inflight, piggyback]);

    expect(postMock).toHaveBeenCalledTimes(1);
  });
});
