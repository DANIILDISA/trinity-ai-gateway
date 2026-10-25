// Derive the downloadable artifact card(s) shown under an assistant answer from
// the message's persisted tool steps (tr-cy0.2.13). A model-authored file is the
// session-end "presentation" of one logical file (D7): a create + N edits of the
// same path collapse into a SINGLE card (latest version), so the card never
// flashes once per edit. Source of truth = `thinkingSteps`, so the card restores
// on reload exactly like the timeline does — no separate persistence needed.

import type { ThinkingStep } from "@/types/thinking";
import type { Artifact } from "@/lib/artifacts";

export interface ToolArtifact {
  // The model-authored filename (also the download key on the endpoint).
  filename: string;
  // Humanized card title, e.g. "python_exam_prep.md" → "Python exam prep".
  title: string;
  // Subtitle, e.g. "Документ · MD".
  typeLabel: string;
}

// Text/document extensions → «Документ»; code extensions → «Код»; everything
// else → «Файл». A focused set; the dedicated filePresentation module
// (tr-cy0.5.1) will own richer typing later.
const DOC_EXTS = new Set(["md", "markdown", "txt", "rst"]);
const CODE_EXTS = new Set([
  "py", "js", "ts", "tsx", "jsx", "json", "sql", "css", "html",
  "sh", "go", "rs", "java", "c", "cpp", "h", "yaml", "yml", "toml",
]);

function extensionOf(filename: string): string {
  const m = /\.([^.]+)$/.exec(filename);
  return (m?.[1] ?? "").toLowerCase();
}

export function toolArtifactTitle(filename: string): string {
  const base = filename.split("/").pop() ?? filename;
  const stem = base.replace(/\.[^.]+$/, "");
  const words = stem.replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
  if (!words) return base;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function toolArtifactTypeLabel(filename: string): string {
  const ext = extensionOf(filename);
  if (!ext) return "Файл";
  // «markdown» reads as MD on the card to match the reference.
  const label = ext === "markdown" ? "MD" : ext.toUpperCase();
  if (DOC_EXTS.has(ext)) return `Документ · ${label}`;
  if (CODE_EXTS.has(ext)) return `Код · ${label}`;
  return `Файл · ${label}`;
}

// File extension → CodeHighlight language hint, so the canvas's «Код» view
// syntax-highlights a code artifact correctly. Markdown extensions get no
// language (they render through the preview/markdown path instead).
const LANG_BY_EXT: Record<string, string> = {
  py: "python",
  js: "javascript",
  ts: "typescript",
  tsx: "tsx",
  jsx: "jsx",
  json: "json",
  sql: "sql",
  css: "css",
  html: "html",
  sh: "bash",
  go: "go",
  rs: "rust",
  java: "java",
  c: "c",
  cpp: "cpp",
  h: "c",
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
};

// Build a canvas-ready Artifact from a tool artifact and its server-fetched
// content (tr-lqn). Document extensions (md/txt/…) open as a markdown preview;
// everything else opens as code with a best-effort language hint. source is
// "server" so the panel knows the content came from the artifact endpoint.
// `meta` carries the version info + the keys the canvas switcher needs to
// refetch another version of this file (tr-qil).
export function toolArtifactToArtifact(
  artifact: ToolArtifact,
  content: string,
  meta?: { version: number; versionCount: number; conversationId: string },
): Artifact {
  const ext = extensionOf(artifact.filename);
  const isDoc = !ext || DOC_EXTS.has(ext);
  const kind = isDoc ? "markdown" : "code";
  return {
    id: `tool-${artifact.filename}`,
    kind,
    language: kind === "code" ? LANG_BY_EXT[ext] ?? ext : undefined,
    title: artifact.title,
    content,
    version: meta?.version,
    versionCount: meta?.versionCount,
    filename: artifact.filename,
    conversationId: meta?.conversationId,
    source: "server",
  };
}

// Tools that only READ a file — they carry a filename for the timeline chip but
// must never surface a download card (tr-7ml).
const READ_ONLY_TOOLS = new Set(["view_file"]);

export function deriveToolArtifacts(steps?: ThinkingStep[]): ToolArtifact[] {
  const seen = new Set<string>();
  const out: ToolArtifact[] = [];
  for (const s of steps ?? []) {
    if (s.status !== "done" || !s.filename) continue;
    if (s.tool && READ_ONLY_TOOLS.has(s.tool)) continue;
    // Dedup on a normalized key (trim, drop a leading "./", collapse repeated
    // slashes) so near-duplicate references to ONE logical file — a create and a
    // later edit that wrote the path slightly differently — fold into a single
    // card (tr-v0l). The first-seen ORIGINAL filename is kept as the download key.
    const key = s.filename.trim().replace(/^\.\//, "").replace(/\/{2,}/g, "/");
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({
      filename: s.filename,
      title: toolArtifactTitle(s.filename),
      typeLabel: toolArtifactTypeLabel(s.filename),
    });
  }
  return out;
}
