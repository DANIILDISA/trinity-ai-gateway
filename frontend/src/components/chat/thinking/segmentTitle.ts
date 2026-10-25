// One-line title for a reasoning segment header (tr-cy0.3.10). Prefers the
// backend summary (heuristic, already normalized); otherwise derives a title
// from the first significant line of the reasoning text, stripping markdown
// heading / bold / list lead so the header reads as plain prose. Kept in sync
// with the backend heuristic (reasoning_summary_service) in spirit.
const LEAD_MARKERS = /^[\s>#*\-+`]+/;
const BOLD = /\*\*([^*]+)\*\*/g;
const INLINE_MD = /[`*]/g;

export function segmentTitle(segment: {
  summary?: string;
  text?: string;
}): string {
  const summary = segment.summary?.trim();
  if (summary) return summary;
  for (const raw of (segment.text ?? "").split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    return line.replace(BOLD, "$1").replace(LEAD_MARKERS, "").replace(INLINE_MD, "").trim();
  }
  return "Размышление";
}
