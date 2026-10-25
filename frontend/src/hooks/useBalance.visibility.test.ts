import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";

import { apiClient } from "@/lib/api";
import { dispatchBalanceRefresh } from "@/lib/balanceEvents";
import { useBalance } from "@/hooks/useBalance";

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

// The poll hook calls into the auth layer to proactively refresh a stale token
// on refocus before issuing the next /users/me request (tr-wd8).
const refreshIfNeededMock = vi.fn().mockResolvedValue("ok");
vi.mock("@/lib/auth", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth")>(
    "@/lib/auth",
  );
  return {
    ...actual,
    refreshIfNeeded: () => refreshIfNeededMock(),
  };
});

const getMock = apiClient.get as unknown as ReturnType<typeof vi.fn>;

const POLL_MS = 30_000;

function setHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", {
    configurable: true,
    get: () => hidden,
  });
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => (hidden ? "hidden" : "visible"),
  });
}

async function flush() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  getMock.mockReset();
  getMock.mockResolvedValue({ id: "u1", balance: "100.00" });
  refreshIfNeededMock.mockClear();
  setHidden(false);
});

afterEach(() => {
  vi.useRealTimers();
  setHidden(false);
});

describe("useBalance — visibility-aware polling (tr-wd8)", () => {
  it("does NOT fire the poll fetch while the tab is hidden", async () => {
    renderHook(() => useBalance());
    // Initial foreground load.
    await flush();
    expect(getMock).toHaveBeenCalledTimes(1);

    // Background the tab, then advance well past several poll intervals.
    setHidden(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * 3);
    });

    // No additional fetches were issued while hidden.
    expect(getMock).toHaveBeenCalledTimes(1);
  });

  it("on becoming visible, refreshes the token (if needed) THEN fetches", async () => {
    renderHook(() => useBalance());
    await flush();
    expect(getMock).toHaveBeenCalledTimes(1);

    setHidden(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * 2);
    });
    expect(getMock).toHaveBeenCalledTimes(1);

    // Refocus the tab.
    setHidden(false);
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(0);
    });
    await flush();

    // Token refresh ran, and a fresh poll fetch followed it.
    expect(refreshIfNeededMock).toHaveBeenCalled();
    expect(getMock).toHaveBeenCalledTimes(2);
  });

  it("keeps polling on the interval for an active foreground tab", async () => {
    renderHook(() => useBalance());
    await flush();
    expect(getMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    expect(getMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    expect(getMock).toHaveBeenCalledTimes(3);
  });
});

describe("useBalance — instant refresh on a balance event (tr-hye6)", () => {
  it("refetches immediately when a balance-refresh event is dispatched", async () => {
    renderHook(() => useBalance());
    await flush();
    expect(getMock).toHaveBeenCalledTimes(1);

    // A top-up landing / chat-send debit signals a change — refetch NOW, without
    // waiting for the 30s poll or a reload.
    await act(async () => {
      dispatchBalanceRefresh();
      await vi.advanceTimersByTimeAsync(0);
    });
    await flush();
    expect(getMock).toHaveBeenCalledTimes(2);
  });
});
