import { describe, it, expect } from "vitest";

import {
  deriveToolArtifacts,
  toolArtifactTitle,
  toolArtifactToArtifact,
  toolArtifactTypeLabel,
} from "./toolArtifact";
import type { ThinkingStep } from "@/types/thinking";

const step = (over: Partial<ThinkingStep>): ThinkingStep => ({
  id: "s",
  title: "t",
  tool: "create_file",
  status: "done",
  ...over,
});

describe("deriveToolArtifacts", () => {
  it("returns one artifact per done file with a humanized title and type label", () => {
    const arts = deriveToolArtifacts([
      step({ id: "tc-0-0", tool: "create_file", filename: "python_exam_prep.md" }),
    ]);
    expect(arts).toEqual([
      {
        filename: "python_exam_prep.md",
        title: "Python exam prep",
        typeLabel: "Документ · MD",
      },
    ]);
  });

  it("dedupes a create + edits of the same file into a single card (D7)", () => {
    const arts = deriveToolArtifacts([
      step({ id: "tc-0-0", tool: "create_file", filename: "python_exam_prep.md" }),
      step({ id: "tc-1-0", tool: "str_replace", filename: "python_exam_prep.md" }),
      step({ id: "tc-2-0", tool: "str_replace", filename: "python_exam_prep.md" }),
    ]);
    expect(arts).toHaveLength(1);
    expect(arts[0].filename).toBe("python_exam_prep.md");
  });

  it("collapses ./-prefix and whitespace variants of one file into one card (tr-v0l)", () => {
    // Live (regenerate): two «Документ · MD» cards for one file because the
    // create and a later edit referenced the path slightly differently. Dedup on
    // a normalized key so near-duplicate paths fold into a single card.
    const arts = deriveToolArtifacts([
      step({ id: "tc-0-0", tool: "create_file", filename: "exam_prep.md" }),
      step({ id: "tc-1-0", tool: "str_replace", filename: "./exam_prep.md" }),
      step({ id: "tc-2-0", tool: "str_replace", filename: " exam_prep.md " }),
    ]);
    expect(arts).toHaveLength(1);
    // The first-seen original filename is kept as the download key.
    expect(arts[0].filename).toBe("exam_prep.md");
  });

  it("keeps multiple distinct files in first-seen order", () => {
    const arts = deriveToolArtifacts([
      step({ id: "a", filename: "notes.md" }),
      step({ id: "b", filename: "main.py" }),
    ]);
    expect(arts.map((a) => a.filename)).toEqual(["notes.md", "main.py"]);
  });

  it("does not card a read-only view_file step (tr-7ml)", () => {
    // view_file returns status ok (→ done) with the filename for the timeline
    // chip, but it READS — it must not surface a download card.
    const arts = deriveToolArtifacts([
      step({ id: "v", tool: "view_file", filename: "notes.md", status: "done" }),
    ]);
    expect(arts).toEqual([]);
  });

  it("cards the create but not a later view_file of the same file", () => {
    const arts = deriveToolArtifacts([
      step({ id: "c", tool: "create_file", filename: "notes.md" }),
      step({ id: "v", tool: "view_file", filename: "notes.md" }),
    ]);
    expect(arts).toHaveLength(1);
    expect(arts[0].filename).toBe("notes.md");
  });

  it("excludes steps that are not done", () => {
    expect(
      deriveToolArtifacts([
        step({ id: "r", filename: "draft.md", status: "running" }),
        step({ id: "e", filename: "broken.md", status: "error" }),
      ]),
    ).toEqual([]);
  });

  it("excludes steps without a filename and handles undefined input", () => {
    expect(deriveToolArtifacts([step({ id: "x", filename: undefined })])).toEqual([]);
    expect(deriveToolArtifacts(undefined)).toEqual([]);
  });
});

describe("toolArtifactTitle", () => {
  it("strips the extension, unslugifies, and capitalizes the first letter", () => {
    expect(toolArtifactTitle("python_exam_prep.md")).toBe("Python exam prep");
  });

  it("uses the basename of a nested path", () => {
    expect(toolArtifactTitle("outputs/python_exam_prep.md")).toBe("Python exam prep");
  });

  it("treats hyphens as separators", () => {
    expect(toolArtifactTitle("release-notes.txt")).toBe("Release notes");
  });
});

describe("toolArtifactTypeLabel", () => {
  it("labels markdown as «Документ · MD»", () => {
    expect(toolArtifactTypeLabel("python_exam_prep.md")).toBe("Документ · MD");
  });

  it("labels a code file as «Код · …»", () => {
    expect(toolArtifactTypeLabel("main.py")).toBe("Код · PY");
  });

  it("falls back to «Файл · …» for an unknown extension", () => {
    expect(toolArtifactTypeLabel("data.bin")).toBe("Файл · BIN");
  });
});

describe("toolArtifactToArtifact (tr-lqn)", () => {
  const tool = (filename: string) => ({
    filename,
    title: toolArtifactTitle(filename),
    typeLabel: toolArtifactTypeLabel(filename),
  });

  it("maps a markdown file to a markdown artifact with no language", () => {
    const a = toolArtifactToArtifact(tool("python_exam_prep.md"), "# Hi");
    expect(a).toMatchObject({
      kind: "markdown",
      language: undefined,
      title: "Python exam prep",
      content: "# Hi",
      source: "server",
    });
  });

  it("maps a code file to a code artifact with a highlight language hint", () => {
    const a = toolArtifactToArtifact(tool("main.py"), "print(1)");
    expect(a).toMatchObject({ kind: "code", language: "python", content: "print(1)" });
  });

  it("falls back to the raw extension as the language for an unknown code type", () => {
    const a = toolArtifactToArtifact(tool("data.bin"), "x");
    expect(a).toMatchObject({ kind: "code", language: "bin" });
  });

  it("carries version metadata + refetch keys when meta is passed (tr-qil)", () => {
    const a = toolArtifactToArtifact(tool("roadmap.md"), "v2 body", {
      version: 2,
      versionCount: 2,
      conversationId: "conv-1",
    });
    expect(a).toMatchObject({
      version: 2,
      versionCount: 2,
      filename: "roadmap.md",
      conversationId: "conv-1",
      source: "server",
    });
  });
});
