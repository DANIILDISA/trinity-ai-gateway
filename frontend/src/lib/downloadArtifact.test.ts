import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { downloadArtifact } from "./downloadArtifact";
import type { Artifact } from "./artifacts";

describe("downloadArtifact", () => {
  let lastBlob: Blob | null = null;
  let downloadName = "";
  let clickSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    lastBlob = null;
    downloadName = "";
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

  it("saves a markdown artifact as title.md with a text/plain blob", () => {
    const art: Artifact = {
      id: "markdown-x",
      kind: "markdown",
      title: "Python exam prep",
      content: "# Подготовка",
      source: "heuristic",
    };
    downloadArtifact(art);

    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(downloadName).toBe("Python exam prep.md");
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(lastBlob).not.toBeNull();
    expect(lastBlob!.type).toContain("text/plain");
  });

  it("names a code artifact by its language extension", () => {
    const art: Artifact = {
      id: "code-y",
      kind: "code",
      language: "python",
      title: "script",
      content: "print(1)",
      source: "heuristic",
    };
    downloadArtifact(art);
    expect(downloadName).toBe("script.py");
  });
});
