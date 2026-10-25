import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { useChat } from "@/hooks/useChat";
import { apiClient } from "@/lib/api";

vi.mock("@/lib/auth", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth")>("@/lib/auth");
  return { ...actual, getAccessToken: () => "test-token" };
});

afterEach(() => {
  vi.restoreAllMocks();
});

const imageDto = (included: boolean) => ({
  id: "img-1",
  message_id: "msg-1",
  full_base64: "AAAA",
  media_type: "image/png",
  created_at: "2026-01-01T00:00:00Z",
  included_in_context: included,
});

const fileDto = (included: boolean) => ({
  id: "file-1",
  message_id: "msg-1",
  filename: "notes.txt",
  media_type: "text/plain",
  kind: "text",
  file_size_bytes: 10,
  token_estimate: 2,
  created_at: "2026-01-01T00:00:00Z",
  included_in_context: included,
});

describe("useChat — image in-context toggle is persisted (tr-kx4f)", () => {
  it("loadConversationImages reads included_in_context from the DTO (false stays false)", async () => {
    // The flag is backend-persisted now; loading must honor it instead of
    // hardcoding includedInContext=true (which silently reset the checkbox).
    vi.spyOn(apiClient, "get").mockResolvedValue([imageDto(false)] as never);
    const { result } = renderHook(() => useChat());

    await act(async () => {
      await result.current.loadConversationImages("conv-1");
    });

    expect(
      result.current.conversationImages.get("msg-1")?.[0].includedInContext,
    ).toBe(false);
  });

  it("toggleImageContext PATCHes the backend and flips the state", async () => {
    vi.spyOn(apiClient, "get").mockResolvedValue([imageDto(true)] as never);
    const patchSpy = vi.spyOn(apiClient, "patch").mockResolvedValue({} as never);
    const { result } = renderHook(() => useChat());
    await act(async () => {
      await result.current.loadConversationImages("conv-1");
    });

    await act(async () => {
      result.current.toggleImageContext("img-1");
    });

    expect(patchSpy).toHaveBeenCalledWith("/api/v1/images/img-1", {
      included_in_context: false,
    });
    await waitFor(() => {
      expect(
        result.current.conversationImages.get("msg-1")?.[0].includedInContext,
      ).toBe(false);
    });
  });

  it("toggleImageContext rolls back the optimistic flip when the PATCH fails", async () => {
    vi.spyOn(apiClient, "get").mockResolvedValue([imageDto(true)] as never);
    vi.spyOn(apiClient, "patch").mockRejectedValue(new Error("network down"));
    const { result } = renderHook(() => useChat());
    await act(async () => {
      await result.current.loadConversationImages("conv-1");
    });

    await act(async () => {
      result.current.toggleImageContext("img-1");
    });

    // The failed PATCH must not leave the UI claiming the image is excluded when
    // the server still has it in context — roll back to the prior (true) state.
    await waitFor(() => {
      expect(
        result.current.conversationImages.get("msg-1")?.[0].includedInContext,
      ).toBe(true);
    });
  });
});

describe("useChat — file in-context toggle is persisted (tr-kx4f)", () => {
  it("loadConversationFiles reads included_in_context from the DTO (false stays false)", async () => {
    vi.spyOn(apiClient, "get").mockResolvedValue([fileDto(false)] as never);
    const { result } = renderHook(() => useChat());

    await act(async () => {
      await result.current.loadConversationFiles("conv-1");
    });

    expect(
      result.current.conversationFiles.get("msg-1")?.[0].includedInContext,
    ).toBe(false);
  });

  it("toggleFileContext PATCHes the backend and flips the state", async () => {
    vi.spyOn(apiClient, "get").mockResolvedValue([fileDto(true)] as never);
    const patchSpy = vi.spyOn(apiClient, "patch").mockResolvedValue({} as never);
    const { result } = renderHook(() => useChat());
    await act(async () => {
      await result.current.loadConversationFiles("conv-1");
    });

    await act(async () => {
      result.current.toggleFileContext("file-1");
    });

    expect(patchSpy).toHaveBeenCalledWith("/api/v1/files/file-1", {
      included_in_context: false,
    });
    await waitFor(() => {
      expect(
        result.current.conversationFiles.get("msg-1")?.[0].includedInContext,
      ).toBe(false);
    });
  });

  it("toggleFileContext rolls back the optimistic flip when the PATCH fails", async () => {
    vi.spyOn(apiClient, "get").mockResolvedValue([fileDto(true)] as never);
    vi.spyOn(apiClient, "patch").mockRejectedValue(new Error("network down"));
    const { result } = renderHook(() => useChat());
    await act(async () => {
      await result.current.loadConversationFiles("conv-1");
    });

    await act(async () => {
      result.current.toggleFileContext("file-1");
    });

    await waitFor(() => {
      expect(
        result.current.conversationFiles.get("msg-1")?.[0].includedInContext,
      ).toBe(true);
    });
  });
});
