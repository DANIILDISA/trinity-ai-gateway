// Shared base64 → Blob helpers for artifact download (epic tr-s3y1 Phase 5, tr-an0g).
// Artifact `content` is RAW text for text artifacts (create_file) but BASE64 for
// binary ones (matplotlib PNGs, and Phase-5 generated files like xlsx/parquet). The
// download path must decode base64 for binary media types — otherwise the saved file
// is the base64 text, not the bytes (audit tr-5ktl: latent bug in downloadToolArtifact).

// Text-ish application/image types whose artifact content is stored as RAW text.
const _TEXT_MEDIA_TYPES = new Set([
  "application/json",
  "application/xml",
  "application/javascript",
  "image/svg+xml",
]);

// True when an artifact's content is base64-encoded bytes (must be decoded on download),
// false when it is raw text. Inferred from the media type: text/* and a few text-ish
// application types are raw; everything else (image/*, application/octet-stream,
// office formats, parquet, …) is base64.
export function isBinaryMediaType(mediaType: string): boolean {
  const m = (mediaType || "").split(";")[0].trim().toLowerCase();
  if (!m || m.startsWith("text/")) return false;
  return !_TEXT_MEDIA_TYPES.has(m);
}

// Decode a base64 string into a Blob of its real bytes (mirrors FileViewer.base64ToBlob).
export function base64ToBlob(base64: string, mediaType: string): Blob {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return new Blob([bytes], { type: mediaType || "application/octet-stream" });
}

// Build a download Blob from artifact content, decoding base64 for binary media types
// and keeping text content verbatim (UTF-8) for text ones.
export function artifactContentToBlob(content: string, mediaType: string): Blob {
  return isBinaryMediaType(mediaType)
    ? base64ToBlob(content, mediaType)
    : new Blob([content], { type: `${mediaType || "text/plain"};charset=utf-8` });
}
