import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { downloadToolArtifact, fetchToolArtifact } from "./downloadToolArtifact";
import { apiClient } from "./api";

vi.mock("./api", () => ({
  apiClient: { get: vi.fn() },
}));

describe("downloadToolArtifact", () => {
  let lastBlob: Blob | null = null;
  let downloadName = "";
  let clickSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    lastBlob = null;
    downloadName = "";
    vi.mocked(apiClient.get).mockReset();
    URL.createObjectURL = vi.fn((b: Blob) => {
      lastBlob = b;
      return "blob:mock";
    }) as unknown as typeof URL.createObjectURL;
    URL.revokeObjectURL = vi.fn();
    clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        downloadName = this.download;
      });
  });

  afterEach(() => {
    clickSpy.mockRestore();
  });

  it("fetches the latest artifact and saves it under its filename with the server media type", async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      filename: "python_exam_prep.md",
      media_type: "text/markdown",
      content: "# Подготовка",
    });

    await downloadToolArtifact("conv-1", "python_exam_prep.md");

    expect(apiClient.get).toHaveBeenCalledWith(
      "/api/v1/chat/conversations/conv-1/artifact?filename=python_exam_prep.md",
    );
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(downloadName).toBe("python_exam_prep.md");
    expect(lastBlob).not.toBeNull();
    expect(lastBlob!.type).toContain("text/markdown");
  });

  it("url-encodes a filename with spaces / non-latin characters", async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      filename: "отчёт по плану.md",
      media_type: "text/markdown",
      content: "x",
    });

    await downloadToolArtifact("conv-2", "отчёт по плану.md");

    expect(apiClient.get).toHaveBeenCalledWith(
      `/api/v1/chat/conversations/conv-2/artifact?filename=${encodeURIComponent(
        "отчёт по плану.md",
      )}`,
    );
    expect(downloadName).toBe("отчёт по плану.md");
  });

  it("base64-decodes a binary artifact to its real bytes (tr-an0g)", async () => {
    const realBytes = [0x50, 0x4b, 0x03, 0x04]; // PK.. — xlsx/zip magic
    const b64 = btoa(String.fromCharCode(...realBytes));
    vi.mocked(apiClient.get).mockResolvedValue({
      filename: "report.xlsx",
      media_type:
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      content: b64,
    });

    await downloadToolArtifact("conv-bin", "report.xlsx");

    expect(lastBlob).not.toBeNull();
    // The decoded blob is the 4 real bytes, NOT the 8-char base64 text (the bug).
    expect(lastBlob!.size).toBe(realBytes.length);
    expect(lastBlob!.type).toContain("spreadsheetml");
  });

  it("keeps a text artifact's content verbatim (no base64 decode)", async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      filename: "data.csv",
      media_type: "text/csv",
      content: "a,b\n1,2\n",
    });

    await downloadToolArtifact("conv-txt", "data.csv");

    expect(lastBlob!.size).toBe(8); // raw UTF-8 bytes of "a,b\n1,2\n"
    expect(lastBlob!.type).toContain("text/csv");
  });

  it("propagates a fetch error so the caller can surface it", async () => {
    vi.mocked(apiClient.get).mockRejectedValue(new Error("404"));
    await expect(downloadToolArtifact("conv-3", "missing.md")).rejects.toThrow();
    expect(clickSpy).not.toHaveBeenCalled();
  });
});

describe("fetchToolArtifact version param (tr-qil)", () => {
  beforeEach(() => {
    vi.mocked(apiClient.get).mockReset();
    vi.mocked(apiClient.get).mockResolvedValue({
      filename: "roadmap.md",
      media_type: "text/markdown",
      content: "v1 body",
      version: 1,
      version_count: 2,
    });
  });

  it("omits the version query when no version is passed (defaults to latest)", async () => {
    await fetchToolArtifact("conv-1", "roadmap.md");
    expect(apiClient.get).toHaveBeenCalledWith(
      "/api/v1/chat/conversations/conv-1/artifact?filename=roadmap.md",
    );
  });

  it("appends &version=N when a specific version is requested", async () => {
    await fetchToolArtifact("conv-1", "roadmap.md", 1);
    expect(apiClient.get).toHaveBeenCalledWith(
      "/api/v1/chat/conversations/conv-1/artifact?filename=roadmap.md&version=1",
    );
  });
});
