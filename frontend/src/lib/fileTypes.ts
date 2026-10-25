// Supported non-image file attachments for the chat composer.
//
// Two families:
//   • "text"  — read as UTF-8 text and (server-side) inlined into the prompt.
//               Works on any file-capable model, no parser plugin.
//   • "pdf"   — sent as a native OpenRouter `file` part; the file-parser plugin
//               converts it (cloudflare-ai → markdown, free).
//   • "docx"  — also sent natively; OpenRouter parses it. We can't read .docx as
//               text in the browser without a new dependency, so it is forwarded
//               as binary and the token count is estimated from its size.
//
// Sending mechanism per family is decided in useChat.buildFileParts; this module
// only classifies files, holds the limits, and estimates tokens.

export type FileKind = "text" | "pdf" | "docx";

export interface SupportedFileType {
  ext: string;
  mime: string;
  kind: FileKind;
}

// Extension → canonical MIME + kind. Extension is the primary key because the
// browser's File.type is unreliable for many code/text formats (often empty).
export const SUPPORTED_FILE_TYPES: SupportedFileType[] = [
  { ext: "pdf", mime: "application/pdf", kind: "pdf" },
  {
    ext: "docx",
    mime: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    kind: "docx",
  },
  { ext: "txt", mime: "text/plain", kind: "text" },
  { ext: "md", mime: "text/markdown", kind: "text" },
  { ext: "json", mime: "application/json", kind: "text" },
  { ext: "csv", mime: "text/csv", kind: "text" },
  { ext: "js", mime: "application/javascript", kind: "text" },
  { ext: "jsx", mime: "text/javascript", kind: "text" },
  { ext: "ts", mime: "application/typescript", kind: "text" },
  { ext: "tsx", mime: "text/x-typescript", kind: "text" },
  { ext: "py", mime: "text/x-python", kind: "text" },
  { ext: "go", mime: "text/x-go", kind: "text" },
  { ext: "java", mime: "text/x-java", kind: "text" },
  { ext: "rs", mime: "text/x-rust", kind: "text" },
  { ext: "yaml", mime: "application/x-yaml", kind: "text" },
  { ext: "yml", mime: "application/x-yaml", kind: "text" },
  { ext: "toml", mime: "application/toml", kind: "text" },
  { ext: "xml", mime: "application/xml", kind: "text" },
  { ext: "html", mime: "text/html", kind: "text" },
  { ext: "css", mime: "text/css", kind: "text" },
  { ext: "sh", mime: "application/x-sh", kind: "text" },
  { ext: "sql", mime: "application/sql", kind: "text" },
];

const BY_EXT = new Map(SUPPORTED_FILE_TYPES.map((t) => [t.ext, t]));

// Limits (§3). Files may be larger than images.
export const MAX_IMAGE_BYTES = 5 * 1024 * 1024; // 5 MB (unchanged from before)
export const MAX_FILE_BYTES = 10 * 1024 * 1024; // 10 MB per file
export const MAX_ATTACHMENTS = 4; // images + files combined

// Token-estimation constants.
export const CHARS_PER_TOKEN = 4;
export const TOKENS_PER_PDF_PAGE = 500;
// .docx is ~zipped XML; uncompressed text roughly tracks the file size. Without
// extraction this is a coarse upper-bound proxy (documented in the module head).
const DOCX_BYTES_PER_TOKEN = 4;

function extOf(filename: string): string {
  const dot = filename.lastIndexOf(".");
  return dot >= 0 ? filename.slice(dot + 1).toLowerCase() : "";
}

/** Classify a dropped/picked file, or null if its extension isn't supported. */
export function classifyFile(file: File): SupportedFileType | null {
  return BY_EXT.get(extOf(file.name)) ?? null;
}

export function isSupportedFile(file: File): boolean {
  return BY_EXT.has(extOf(file.name));
}

/** Build the `accept` attribute for the hidden <input type=file>. */
export function buildAccept(opts: { images: boolean; files: boolean }): string {
  const parts: string[] = [];
  if (opts.images) parts.push("image/*");
  if (opts.files) {
    for (const t of SUPPORTED_FILE_TYPES) parts.push(`.${t.ext}`);
  }
  return parts.join(",");
}

export function estimateTextTokens(text: string): number {
  return Math.ceil(text.length / CHARS_PER_TOKEN);
}

// Approximate a PDF's page count by scanning for page objects in the raw bytes,
// then bill ~500 tokens/page. Falls back to a size-based estimate when the scan
// finds nothing (e.g. compressed object streams). Best-effort, never throws.
export function estimatePdfTokens(bytes: Uint8Array): number {
  let pages = 0;
  try {
    // Latin-1 view so byte offsets map 1:1 to string indices for the regex.
    let latin1 = "";
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
      latin1 += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
    }
    const matches = latin1.match(/\/Type\s*\/Page[^s]/g);
    pages = matches ? matches.length : 0;
  } catch {
    pages = 0;
  }
  if (pages <= 0) {
    // ~3 KB of PDF per page is a rough average for text documents.
    pages = Math.max(1, Math.round(bytes.length / 3000));
  }
  return pages * TOKENS_PER_PDF_PAGE;
}

export function estimateDocxTokens(sizeBytes: number): number {
  return Math.max(1, Math.ceil(sizeBytes / DOCX_BYTES_PER_TOKEN));
}

/** Human-readable file size for previews/tooltips. */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} КБ`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`;
}
