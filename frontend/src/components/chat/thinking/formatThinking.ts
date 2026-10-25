// Header formatters for the thinking block. Kept pure so they are trivially
// unit-tested; the live ticking lives in useThinkingClock.

export function formatDurationSec(ms: number): string {
  const sec = Math.max(0, Math.round(ms / 1000));
  return `${sec}с`;
}

// Russian plural selector: «1 токен», «2 токена», «5 токенов» (and teens 11–14
// are always the «many» form).
function pluralRu(n: number, one: string, few: string, many: string): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

export function formatReasoningTokens(tokens: number): string {
  if (tokens >= 1000) {
    const k = tokens / 1000;
    const value = k.toFixed(1).replace(/\.0$/, "");
    // The abbreviated k-form always reads «Nk токенов».
    return `${value}k токенов`;
  }
  return `${tokens} ${pluralRu(tokens, "токен", "токена", "токенов")}`;
}

export function buildHeaderMeta(opts: {
  durationMs?: number;
  reasoningTokens?: number;
}): string {
  const parts: string[] = [];
  if (opts.durationMs != null) parts.push(formatDurationSec(opts.durationMs));
  if (opts.reasoningTokens != null && opts.reasoningTokens > 0) {
    parts.push(formatReasoningTokens(opts.reasoningTokens));
  }
  return parts.join(" · ");
}
