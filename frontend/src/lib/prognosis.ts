import type { ModelInfo } from "@/types/api";

// Flat per-image token estimate, mirrors the backend's _IMAGE_TOKEN_ESTIMATE.
export const IMAGE_TOKENS = 1000;
// Conservative average output estimate for the per-request prognosis.
export const OUTPUT_TOKENS_ESTIMATE = 500;
// Cheap tiktoken approximation: ~4 chars per token.
export const CHARS_PER_TOKEN = 4;

function priceNum(s: string | undefined): number {
  const n = Number.parseFloat(s ?? "");
  return Number.isFinite(n) ? n : 0;
}

export interface PrognosisInput {
  // Sum of input+output tokens already in the branch's context.
  historyTokens: number;
  // The composed / edited text whose tokens we estimate at ~4 chars each.
  text: string;
  // Count of images that will be in context (each ~IMAGE_TOKENS).
  images: number;
  // Files in context; only the token estimate matters here.
  files: { tokenEstimate: number }[];
  // Selected model — supplies the per-million prices. Undefined → zero cost.
  model: ModelInfo | undefined;
}

export interface PrognosisResult {
  inputTokens: number;
  costRub: number;
}

// The single source of truth for the per-request token + ruble forecast,
// shared by the composer, the inline edit box, and the analytics panel so the
// three never drift. Extracted from MicroAnalytics' prognosis block.
export function estimatePrognosis({
  historyTokens,
  text,
  images,
  files,
  model,
}: PrognosisInput): PrognosisResult {
  const fileTokens = files.reduce((s, f) => s + f.tokenEstimate, 0);
  const inputTokens =
    historyTokens +
    Math.ceil(text.length / CHARS_PER_TOKEN) +
    images * IMAGE_TOKENS +
    fileTokens;

  const inputPrice = priceNum(model?.input_price_per_m);
  const outputPrice = priceNum(model?.output_price_per_m);
  const costRub =
    (inputTokens / 1_000_000) * inputPrice +
    (OUTPUT_TOKENS_ESTIMATE / 1_000_000) * outputPrice;

  return { inputTokens, costRub };
}
