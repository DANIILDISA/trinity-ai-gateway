import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { apiClient } from "@/lib/api";
import type { ConversationListResponse } from "@/types/api";
import { useConversations } from "./useConversations";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    apiClient: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  };
});

const getMock = apiClient.get as unknown as ReturnType<typeof vi.fn>;

function page(items: number, total: number, pageNo: number): ConversationListResponse {
  return {
    items: Array.from({ length: items }, (_, i) => ({
      id: `p${pageNo}-${i}`,
      title: `Чат ${pageNo}-${i}`,
      model_id: "openai/gpt-4.1-mini",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    })),
    total,
    page: pageNo,
    per_page: 100,
  };
}

beforeEach(() => {
  getMock.mockReset();
});

describe("useConversations.loadAll", () => {
  it("paginates until total is reached and stores the full list", async () => {
    // Initial mount load() (per_page=50) + loadAll() pages of 100.
    getMock.mockImplementation((path: string) => {
      // NB: "per_page=100" contains the substring "page=1", so match exact frags.
      if (path.includes("per_page=50")) return Promise.resolve(page(50, 150, 1));
      if (path.includes("page=1&per_page=100")) return Promise.resolve(page(100, 150, 1));
      if (path.includes("page=2&per_page=100")) return Promise.resolve(page(50, 150, 2));
      return Promise.resolve(page(0, 150, 99));
    });

    const { result } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(async () => {
      await result.current.loadAll();
    });

    expect(result.current.conversations).toHaveLength(150);
    expect(result.current.loadedAll).toBe(true);
    expect(result.current.isLoadingAll).toBe(false);
  });

  it("stops when a page returns no items", async () => {
    getMock.mockImplementation((path: string) => {
      if (path.includes("per_page=50")) return Promise.resolve(page(0, 5, 1));
      if (path.includes("page=1&per_page=100")) return Promise.resolve(page(3, 999, 1));
      return Promise.resolve(page(0, 999, 2)); // page 2 empty → stop
    });

    const { result } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await act(async () => {
      await result.current.loadAll();
    });
    expect(result.current.conversations).toHaveLength(3);
    expect(result.current.loadedAll).toBe(true);
  });
});
