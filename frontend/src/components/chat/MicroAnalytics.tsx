"use client";

import { memo, useMemo } from "react";

import {
  CHARS_PER_TOKEN,
  IMAGE_TOKENS,
  estimatePrognosis,
} from "@/lib/prognosis";
import { formatCost, formatTokenCount } from "@/lib/formatting";
import { micro_to_cost_string } from "@/lib/microRub";
import type { AttachedFile, AttachedImage, ChatMessage } from "@/hooks/useChat";
import type {
  BranchTotals,
  ContextFile,
  ContextImage,
  ModelInfo,
} from "@/types/api";

export interface MicroAnalyticsProps {
  messages: ChatMessage[];
  conversationImages: Map<string, ContextImage[]>;
  conversationFiles: Map<string, ContextFile[]>;
  inputValue: string;
  pendingImages: AttachedImage[];
  pendingFiles: AttachedFile[];
  selectedModel: ModelInfo | undefined;
  // All-branches aggregate (sums over every message incl. inactive siblings).
  // Undefined / un-branched conversation → the «Все ветки» section is hidden
  // and the legacy single-view layout is preserved.
  branchTotals?: BranchTotals;
  // Visibility is owned by the composer (the analytics button toggles it); this
  // closes the panel from its own header.
  onClose: () => void;
}

// Parses a per-message cost string ("123.4500") for the running chat-spend sum.
// (The shared price→cost math lives in estimatePrognosis; this is unrelated.)
function priceNum(s: string | undefined): number {
  const n = Number.parseFloat(s ?? "");
  return Number.isFinite(n) ? n : 0;
}

function ChartIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M3 3v18h18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      <rect x="7" y="11" width="3" height="6" stroke="currentColor" strokeWidth="2" />
      <rect x="13" y="7" width="3" height="10" stroke="currentColor" strokeWidth="2" />
    </svg>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2 py-1">
      <span className="text-xs text-neutral-500">{label}</span>
      <span className="font-mono text-sm tabular-nums text-neutral-100">
        {value}
      </span>
    </div>
  );
}

function MicroAnalyticsInner({
  messages,
  conversationImages,
  conversationFiles,
  inputValue,
  pendingImages,
  pendingFiles,
  selectedModel,
  branchTotals,
  onClose,
}: MicroAnalyticsProps) {
  const metrics = useMemo(() => {
    let historyTokens = 0;
    let inputTokenTotal = 0;
    let outputTokenTotal = 0;
    let messagesWithTokens = 0;
    let chatCost = 0;
    let webSearchCount = 0;
    for (const m of messages) {
      if (m.tokensUsed) {
        inputTokenTotal += m.tokensUsed.input;
        outputTokenTotal += m.tokensUsed.output;
        historyTokens += m.tokensUsed.input + m.tokensUsed.output;
        messagesWithTokens += 1;
      }
      if (m.actualCost) chatCost += priceNum(m.actualCost);
      if (m.role === "assistant" && m.webSearchEnabled) webSearchCount += 1;
    }

    let includedImages = 0;
    for (const imgs of conversationImages.values()) {
      for (const img of imgs) if (img.includedInContext) includedImages += 1;
    }

    // Real image-token accounting (Fix 3). A flat per-image estimate (1000) is
    // wrong — a single image routinely costs 2400+ input tokens. OpenRouter
    // reports no per-part breakdown, so we back-calculate from the reported
    // prompt tokens: image_tokens ≈ real_input − estimated_text − file_tokens.
    //
    // We take a single measurement at the FIRST assistant response whose context
    // already contained images. Images are re-injected into every subsequent
    // request, so summing per-turn deltas would double-count them; the earliest
    // response also carries the least accumulated history, making the text
    // estimate (and thus the image remainder) the cleanest. The transcript is
    // walked in order — robust to consecutive user turns with no reply between.
    let realImageTokens = 0;
    let measuredImageCount = 0;
    let measured = false;
    // Running estimate of the text + file tokens already in context as we walk,
    // and the count of included images seen so far (both reflect the context an
    // assistant message at the current position would have been sent).
    let contextTextTokens = 0;
    let contextFileTokens = 0;
    let contextImageCount = 0;
    for (const m of messages) {
      if (
        !measured &&
        m.role === "assistant" &&
        m.tokensUsed &&
        contextImageCount > 0
      ) {
        realImageTokens = Math.max(
          0,
          m.tokensUsed.input - contextTextTokens - contextFileTokens,
        );
        measuredImageCount = contextImageCount;
        measured = true;
      }
      // Accumulate this message into the running context for any later response.
      // Mirror the backend's per-message overhead so the text estimate tracks
      // count_message_tokens a little more closely.
      contextTextTokens += Math.ceil(m.content.length / CHARS_PER_TOKEN) + 4;
      if (m.role === "user") {
        for (const img of conversationImages.get(m.id) ?? []) {
          if (img.includedInContext) contextImageCount += 1;
        }
        for (const f of conversationFiles.get(m.id) ?? []) {
          if (f.includedInContext) contextFileTokens += f.token_estimate;
        }
      }
    }
    // Included images introduced after the measured response (or before any
    // response exists, e.g. just sent pre-sync) fall back to the flat estimate
    // so the figure never under-reports.
    const unmeasuredImages = Math.max(0, includedImages - measuredImageCount);
    const imageTokens = realImageTokens + unmeasuredImages * IMAGE_TOKENS;

    // Files sent in this conversation (persisted), counted like images. Only
    // those still toggled into context contribute, matching the image count.
    let sentFileCount = 0;
    let sentFileTokens = 0;
    for (const files of conversationFiles.values()) {
      for (const f of files) {
        if (!f.includedInContext) continue;
        sentFileCount += 1;
        sentFileTokens += f.token_estimate;
      }
    }

    const prog = estimatePrognosis({
      historyTokens,
      text: inputValue,
      images: includedImages + pendingImages.length,
      files: [
        ...Array.from(conversationFiles.values()).flatMap((fs) =>
          fs
            .filter((f) => f.includedInContext)
            .map((f) => ({ tokenEstimate: f.token_estimate })),
        ),
        ...pendingFiles.map((f) => ({ tokenEstimate: f.tokenEstimate })),
      ],
      model: selectedModel,
    });
    const prognosisCost = prog.costRub;

    const avgTokensPerMessage =
      messagesWithTokens > 0
        ? Math.round(historyTokens / messagesWithTokens)
        : 0;

    return {
      historyTokens,
      inputTokenTotal,
      outputTokenTotal,
      messagesWithTokens,
      avgTokensPerMessage,
      chatCost,
      includedImages,
      imageTokens,
      sentFileCount,
      sentFileTokens,
      webSearchCount,
      prognosisCost,
    };
  }, [
    messages,
    conversationImages,
    conversationFiles,
    inputValue,
    pendingImages,
    pendingFiles,
    selectedModel,
  ]);

  // A conversation is "branched" when any rendered message has more than one
  // version (a regenerate/edit produced a sibling). Only then do we split the
  // panel into «Текущая ветка» (live) + «Все ветки» (branchTotals).
  const isBranched = useMemo(
    () => messages.some((m) => (m.versionCount ?? 1) > 1),
    [messages],
  );

  // Compact "1 : 1.4" ratio with one decimal; empty string while there's no
  // real token data so we don't print misleading zeroes.
  const ioRatio = (() => {
    if (metrics.inputTokenTotal === 0 && metrics.outputTokenTotal === 0) return "—";
    if (metrics.inputTokenTotal === 0) return `0 : 1`;
    const r = metrics.outputTokenTotal / metrics.inputTokenTotal;
    return `1 : ${r.toFixed(2)}`;
  })();

  return (
    <aside className="flex h-full w-[320px] shrink-0 flex-col p-4">
      <div className="flex h-full flex-col rounded-lg border border-neutral-800 bg-neutral-800 p-4 text-neutral-100">
        <div className="mb-3 flex items-center justify-between">
          <span className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-widest text-neutral-500">
            <ChartIcon />
            Аналитика
          </span>
          <button
            type="button"
            onClick={onClose}
            title="Закрыть"
            aria-label="Закрыть аналитику"
            className="inline-flex h-6 w-6 items-center justify-center rounded-md text-neutral-500 transition-colors hover:bg-neutral-800 hover:text-neutral-200"
          >
            ✕
          </button>
        </div>

        <section className="flex flex-col">
          <SectionLabel>Контекст</SectionLabel>
          <Row label="Сообщений с токенами" value={String(metrics.messagesWithTokens)} />
          <Row label="Токены контекста" value={formatTokenCount(metrics.historyTokens)} />
          <Row
            label="Сред. токенов/сообщ."
            value={formatTokenCount(metrics.avgTokensPerMessage)}
          />
        </section>

        <section className="mt-4 flex flex-col border-t border-neutral-800 pt-3">
          <SectionLabel>Токены</SectionLabel>
          <Row label="Вход" value={formatTokenCount(metrics.inputTokenTotal)} />
          <Row label="Выход" value={formatTokenCount(metrics.outputTokenTotal)} />
          <Row label="Соотношение в:в" value={ioRatio} />
        </section>

        <section className="mt-4 flex flex-col border-t border-neutral-800 pt-3">
          <SectionLabel>Использование</SectionLabel>
          <Row label="Изображений" value={String(metrics.includedImages)} />
          <Row
            label="Токены изображений"
            value={formatTokenCount(metrics.imageTokens)}
          />
          <Row label="Файлов" value={String(metrics.sentFileCount)} />
          <Row
            label="Токены файлов"
            value={`~${formatTokenCount(metrics.sentFileTokens)}`}
          />
          <Row label="Веб-поиск" value={String(metrics.webSearchCount)} />
        </section>

        <section className="mt-4 border-t border-neutral-800 pt-3">
          {isBranched ? <SectionLabel>Текущая ветка</SectionLabel> : null}
          <div className="flex items-baseline justify-between gap-2">
            <span className="text-xs text-neutral-400">Стоимость чата</span>
            <span className="font-mono text-sm tabular-nums text-neutral-100">
              {metrics.chatCost.toFixed(4)} ₽
            </span>
          </div>
          <div className="mt-1 flex items-baseline justify-between gap-2">
            <span className="text-xs text-neutral-300">Прогноз запроса</span>
            <span className="font-mono text-base font-semibold tabular-nums text-emerald-400">
              ~{metrics.prognosisCost.toFixed(4)} ₽
            </span>
          </div>
        </section>

        {isBranched && branchTotals ? (
          <section className="mt-4 flex flex-col border-t border-neutral-800 pt-3 opacity-80">
            <SectionLabel>Все ветки</SectionLabel>
            <Row
              label="Сообщений"
              value={String(branchTotals.message_count)}
            />
            <Row
              label="Генераций"
              value={String(branchTotals.generation_count)}
            />
            <Row
              label="Вход"
              value={formatTokenCount(branchTotals.total_input_tokens)}
            />
            <Row
              label="Выход"
              value={formatTokenCount(branchTotals.total_output_tokens)}
            />
            <Row
              label="Мышление"
              value={formatTokenCount(branchTotals.total_reasoning_tokens)}
            />
            <div className="mt-1 flex items-baseline justify-between gap-2">
              <span className="text-xs text-neutral-400">Всего потрачено</span>
              <span className="font-mono text-sm tabular-nums text-neutral-300">
                {formatCost(
                  micro_to_cost_string(branchTotals.total_cost_micro_rub),
                )}
              </span>
            </div>
          </section>
        ) : null}
      </div>
    </aside>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="mb-1 text-[10px] font-medium uppercase tracking-wider text-neutral-600">
      {children}
    </h3>
  );
}

export const MicroAnalytics = memo(MicroAnalyticsInner);
MicroAnalytics.displayName = "MicroAnalytics";

export default MicroAnalytics;
