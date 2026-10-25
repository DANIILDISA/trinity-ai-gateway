import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type {
  GalleryArtifactsResponse,
  GalleryFilesResponse,
  GalleryResponse,
} from "@/types/api";

// Route apiClient.get by URL so the default (uploads) mode and the opt-in
// artifacts mode can be asserted independently.
const get = vi.fn();
vi.mock("@/lib/api", () => ({
  apiClient: { get: (url: string) => get(url) },
}));

const downloadToolArtifact = vi.fn().mockResolvedValue(undefined);
vi.mock("@/lib/downloadToolArtifact", () => ({
  downloadToolArtifact: (...args: unknown[]) => downloadToolArtifact(...args),
}));

// IntersectionObserver is used for infinite scroll; stub it so the page mounts.
class IOStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
beforeEach(() => {
  get.mockReset();
  downloadToolArtifact.mockClear();
  (globalThis as { IntersectionObserver?: unknown }).IntersectionObserver =
    IOStub as unknown as typeof IntersectionObserver;
});

const emptyImages: GalleryResponse = {
  items: [],
  total: 0,
  page: 1,
  has_more: false,
};

const uploadsOneFile: GalleryFilesResponse = {
  items: [
    {
      id: "file-1",
      filename: "uploaded.txt",
      media_type: "text/plain",
      kind: "text",
      file_size_bytes: 6,
      token_estimate: 2,
      created_at: "2026-06-01T10:00:00Z",
      conversation_id: "conv-1",
      archived: false,
    },
  ],
  total: 1,
  page: 1,
  has_more: false,
};

const artifactsOne: GalleryArtifactsResponse = {
  items: [
    {
      group_id: "grp-1",
      filename: "roadmap.md",
      media_type: "text/markdown",
      size_bytes: 42,
      version: 2,
      version_count: 2,
      conversation_id: "conv-9",
      created_at: "2026-06-02T10:00:00Z",
    },
  ],
  total: 1,
  page: 1,
  has_more: false,
};

const artifactsEmpty: GalleryArtifactsResponse = {
  items: [],
  total: 0,
  page: 1,
  has_more: false,
};

function routeGet(artifacts: GalleryArtifactsResponse) {
  get.mockImplementation((url: string) => {
    if (url.includes("/images/gallery")) return Promise.resolve(emptyImages);
    if (url.includes("/files/artifacts")) return Promise.resolve(artifacts);
    if (url.includes("/files/gallery")) return Promise.resolve(uploadsOneFile);
    return Promise.reject(new Error(`unexpected url ${url}`));
  });
}

import FilesPage from "./page";

describe("Files gallery — two modes", () => {
  it("defaults to the uploads view and does not fetch artifacts", async () => {
    routeGet(artifactsEmpty);
    render(<FilesPage />);

    // The uploaded file is shown (default uploads mode).
    await waitFor(() =>
      expect(screen.getByText("uploaded.txt")).toBeInTheDocument(),
    );
    // No artifacts endpoint hit while in the default mode.
    expect(get.mock.calls.some(([u]) => String(u).includes("/files/artifacts"))).toBe(
      false,
    );
  });

  it("switching to «Созданные ИИ» fetches and lists AI artifacts", async () => {
    routeGet(artifactsOne);
    render(<FilesPage />);
    await waitFor(() =>
      expect(screen.getByText("uploaded.txt")).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByRole("tab", { name: /Созданные ИИ/ }));

    await waitFor(() =>
      expect(screen.getByText("roadmap.md")).toBeInTheDocument(),
    );
    expect(
      get.mock.calls.some(([u]) => String(u).includes("/files/artifacts")),
    ).toBe(true);
    // The uploaded file is no longer shown in the artifacts mode.
    expect(screen.queryByText("uploaded.txt")).toBeNull();
  });

  it("shows an empty state in the artifacts mode when there are none", async () => {
    routeGet(artifactsEmpty);
    render(<FilesPage />);
    await waitFor(() =>
      expect(screen.getByText("uploaded.txt")).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByRole("tab", { name: /Созданные ИИ/ }));

    await waitFor(() =>
      expect(screen.getByText(/ещё не создавал/i)).toBeInTheDocument(),
    );
  });

  it("downloads an artifact via the per-conversation endpoint", async () => {
    routeGet(artifactsOne);
    render(<FilesPage />);
    await waitFor(() =>
      expect(screen.getByText("uploaded.txt")).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole("tab", { name: /Созданные ИИ/ }));
    await waitFor(() =>
      expect(screen.getByText("roadmap.md")).toBeInTheDocument(),
    );

    await userEvent.click(
      screen.getByRole("button", { name: /Скачать roadmap.md/ }),
    );
    expect(downloadToolArtifact).toHaveBeenCalledWith("conv-9", "roadmap.md");
  });
});
