// Artifact model + a deterministic, pure heuristic that lifts a large
// self-contained markdown/code block out of an assistant reply so it can be
// shown as a card and opened in the canvas panel. Trinity proxies the model
// (no inference of its own), so on the "light" path this parsing happens on the
// client. When the backend later emits real (server) artifacts, the caller
// passes { skip: true } to disable the heuristic for that message.

export type ArtifactKind = "markdown" | "code";

export interface Artifact {
  id: string;
  kind: ArtifactKind;
  language?: string;
  title: string;
  content: string;
  // Server artifacts only (tr-qil): the version being shown and how many versions
  // exist for that file. `versionCount > 1` enables the canvas <n/m> switcher.
  version?: number;
  versionCount?: number;
  // Keys the switcher needs to refetch another version of THIS file from the
  // artifact endpoint. Set only for server artifacts opened from a tool card.
  filename?: string;
  conversationId?: string;
  source: "heuristic" | "server";
}

export interface ExtractResult {
  stripped: string;
  artifacts: Artifact[];
}

// Tunable thresholds — a fenced block is lifted when it is a markdown document,
// or large enough to dominate the message rather than illustrate it inline.
const MIN_ARTIFACT_CHARS = 600;
const MIN_ARTIFACT_LINES = 15;
const MARKDOWN_LANGS = new Set(["markdown", "md"]);

const EXT_BY_LANG: Record<string, string> = {
  python: ".py",
  javascript: ".js",
  typescript: ".ts",
  tsx: ".tsx",
  jsx: ".jsx",
  json: ".json",
  sql: ".sql",
  css: ".css",
  html: ".html",
  bash: ".sh",
  go: ".go",
  rust: ".rs",
  java: ".java",
  c: ".c",
  cpp: ".cpp",
  yaml: ".yaml",
  diff: ".diff",
};

const FILENAME_RE = /^[\w.\-/]+\.[a-z0-9]{1,8}$/i;

interface RawBlock {
  lang: string;
  content: string;
  startLine: number;
  endLine: number;
}

// FNV-1a (32-bit) — small, dependency-free, deterministic. Used only for stable
// React keys within a message, not for security.
function hashString(input: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < input.length; i++) {
    h ^= input.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(36);
}

// Scan for fenced code blocks honoring CommonMark fence length: a block opened
// with N backticks/tildes only closes on a line of >= N of the same char. This
// lets a ````markdown document contain inner ``` code fences without closing it.
function scanFencedBlocks(lines: string[]): RawBlock[] {
  const blocks: RawBlock[] = [];
  let i = 0;
  while (i < lines.length) {
    const open = /^\s*(`{3,}|~{3,})\s*([^\s`~]*)\s*$/.exec(lines[i]);
    if (!open) {
      i++;
      continue;
    }
    const fenceChar = open[1][0];
    const fenceLen = open[1].length;
    const lang = open[2] ?? "";
    let j = i + 1;
    let closed = false;
    for (; j < lines.length; j++) {
      const close = /^\s*(`{3,}|~{3,})\s*$/.exec(lines[j]);
      if (close && close[1][0] === fenceChar && close[1].length >= fenceLen) {
        closed = true;
        break;
      }
    }
    const endLine = closed ? j : lines.length - 1;
    const content = lines.slice(i + 1, closed ? j : lines.length).join("\n");
    blocks.push({ lang, content, startLine: i, endLine });
    i = endLine + 1;
  }
  return blocks;
}

function qualifies(block: RawBlock): boolean {
  if (MARKDOWN_LANGS.has(block.lang.toLowerCase())) return true;
  if (block.content.length > MIN_ARTIFACT_CHARS) return true;
  if (block.content.split("\n").length > MIN_ARTIFACT_LINES) return true;
  return false;
}

function hasMarkdownDocShape(content: string): boolean {
  const hasHeading = /^#{1,3}\s+\S/m.test(content);
  const hasTable = /^\s*\|?[\s:|-]*-{3,}[\s:|-]*\|/m.test(content);
  return hasHeading && hasTable;
}

function blockKind(block: RawBlock): ArtifactKind {
  if (MARKDOWN_LANGS.has(block.lang.toLowerCase())) return "markdown";
  if (!block.lang && hasMarkdownDocShape(block.content)) return "markdown";
  return "code";
}

// Index of a heading/line directly above the fence that is purely a filename
// (e.g. "# Python_exam_prep.md"); -1 if none.
function precedingFilenameIndex(lines: string[], startLine: number): number {
  let k = startLine - 1;
  while (k >= 0 && lines[k].trim() === "") k--;
  if (k < 0) return -1;
  const text = lines[k]
    .trim()
    .replace(/^#{1,6}\s*/, "")
    .replace(/^[`*]+|[`*]+$/g, "")
    .trim();
  return FILENAME_RE.test(text) ? k : -1;
}

function filenameToTitle(line: string): string {
  const name =
    line
      .trim()
      .replace(/^#{1,6}\s*/, "")
      .replace(/^[`*]+|[`*]+$/g, "")
      .trim()
      .split("/")
      .pop() ?? "";
  return name
    .replace(/\.[a-z0-9]+$/i, "")
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function deriveTitle(
  block: RawBlock,
  kind: ArtifactKind,
  lines: string[],
  filenameIdx: number,
): string {
  if (filenameIdx >= 0) return filenameToTitle(lines[filenameIdx]);
  const heading = /^#{1,2}\s+(.+?)\s*$/m.exec(block.content);
  if (heading) return heading[1].trim();
  if (kind === "markdown") return "Документ";
  return block.lang || "Код";
}

export function extractArtifacts(
  content: string,
  opts?: { skip?: boolean },
): ExtractResult {
  if (!content || opts?.skip) return { stripped: content ?? "", artifacts: [] };

  const normalized = content.replace(/\r\n/g, "\n");
  const lines = normalized.split("\n");
  const blocks = scanFencedBlocks(lines).filter(qualifies);
  if (blocks.length === 0) return { stripped: content, artifacts: [] };

  const remove = new Set<number>();
  const artifacts: Artifact[] = [];

  for (const block of blocks) {
    for (let l = block.startLine; l <= block.endLine; l++) remove.add(l);

    const kind = blockKind(block);
    const filenameIdx = precedingFilenameIndex(lines, block.startLine);
    if (filenameIdx >= 0) remove.add(filenameIdx);

    const title = deriveTitle(block, kind, lines, filenameIdx);
    const language =
      kind === "code" ? block.lang.toLowerCase() || undefined : undefined;
    const id = `${kind}-${hashString(
      `${kind} ${language ?? ""} ${block.content.trim()}`,
    )}`;

    artifacts.push({
      id,
      kind,
      language,
      title,
      content: block.content,
      source: "heuristic",
    });
  }

  const stripped = lines
    .filter((_, idx) => !remove.has(idx))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();

  return { stripped, artifacts };
}

export function artifactExtension(artifact: Artifact): string {
  if (artifact.kind === "markdown") return ".md";
  return EXT_BY_LANG[(artifact.language ?? "").toLowerCase()] ?? ".txt";
}

export function artifactLabel(artifact: Artifact): string {
  if (artifact.kind === "markdown") return "Документ · MD";
  const lang = (artifact.language ?? "").toUpperCase();
  return lang ? `Код · ${lang}` : "Код";
}
