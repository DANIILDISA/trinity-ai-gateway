import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

import { ApiRequestError, apiClient } from "@/lib/api";
import { clearAuth } from "@/lib/auth";
import { useAuth } from "@/hooks/useAuth";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));

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
const getMock = apiClient.get as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  postMock.mockReset();
  getMock.mockReset();
  clearAuth();
});

afterEach(() => {
  clearAuth();
});

describe("useAuth hydration — transient vs confirmed-logout", () => {
  it("a transient refresh failure (503) sets connectionError and stays NOT-unauthenticated", async () => {
    // No token -> hydrate refreshes -> backend overloaded.
    postMock.mockRejectedValue(new ApiRequestError("перегрузка", 503));

    const { result } = renderHook(() => useAuth());

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    // The session is NOT confirmed dead — show the reconnect state, never /login.
    expect(result.current.connectionError).toBe(true);
    expect(result.current.isAuthenticated).toBe(false);
  });

  it("a 401 refresh is a confirmed logout: connectionError stays false", async () => {
    postMock.mockRejectedValue(new ApiRequestError("нет сессии", 401));

    const { result } = renderHook(() => useAuth());

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.connectionError).toBe(false);
    expect(result.current.isAuthenticated).toBe(false);
  });

  it("a successful refresh + /me authenticates with no connectionError", async () => {
    postMock.mockResolvedValue({
      access_token: "tok",
      token_type: "bearer",
      user: { id: "u1" },
    });
    getMock.mockResolvedValue({ id: "u1", email: "a@b.c" });

    const { result } = renderHook(() => useAuth());

    await waitFor(() => expect(result.current.isAuthenticated).toBe(true));
    expect(result.current.connectionError).toBe(false);
  });

  it("a transient /users/me failure keeps the user logged in (no clearAuth, no logout)", async () => {
    // Refresh succeeds, but /me hits a transient 503 — must not be treated as 401.
    postMock.mockResolvedValue({
      access_token: "tok",
      token_type: "bearer",
      user: { id: "u1" },
    });
    getMock.mockRejectedValue(new ApiRequestError("перегрузка", 503));

    const { result } = renderHook(() => useAuth());

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.connectionError).toBe(true);
    expect(result.current.isAuthenticated).toBe(false);
  });
});
