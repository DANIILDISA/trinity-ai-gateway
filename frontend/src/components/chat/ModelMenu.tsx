"use client";

import { useEffect, useRef, useState } from "react";

import { ModelTierIcon, getTierInfo } from "@/components/ui/ModelTierIcon";
import { ToggleVisual } from "@/components/ui/ToggleSwitch";
import type { ModelInfo, ThinkingEffort } from "@/types/api";

function priceNum(s: string | undefined): number {
  const n = Number.parseFloat(s ?? "");
  return Number.isFinite(n) ? n : 0;
}

// Compact trigger label: drop the redundant "Provider: " prefix so the name fits
// the composer's narrow model control without an ugly mid-word truncation
// ("Anthropic: Claude Fable Large" → "Claude Fable Large"), mirroring how the
// sidebar shows model names. The full name stays available via the trigger's
// title tooltip and the model selector. Keeps the original casing/punctuation of
// the curated name (unlike formatModelName, which title-cases a raw id).
function shortModelName(name: string): string {
  const i = name.indexOf(": ");
  return i > 0 ? name.slice(i + 2) : name;
}

const EFFORT_OPTIONS: { value: ThinkingEffort; label: string }[] = [
  { value: "low", label: "Низкое" },
  { value: "medium", label: "Среднее" },
  { value: "high", label: "Высокое" },
];

const EFFORT_LABEL: Record<ThinkingEffort, string> = {
  low: "Низкое",
  medium: "Среднее",
  high: "Высокое",
};

export interface ModelMenuProps {
  selectedModel: ModelInfo | undefined;
  disabled: boolean;
  thinkingEnabled: boolean;
  thinkingEffort: ThinkingEffort;
  toolsEnabled: boolean;
  onOpenModelSelector: () => void;
  onToggleThinking: () => void;
  onSetThinkingEffort: (effort: ThinkingEffort) => void;
  onToggleTools: () => void;
  // Whether the model picker (opened from "Сменить модель") is open, so focus can
  // return to this trigger when it closes (a11y, tr-34yj).
  modelSelectorOpen?: boolean;
}

function ChevronDown() {
  return (
    <svg
      className="h-4 w-4 shrink-0 text-neutral-500"
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      aria-hidden="true"
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4 4-4" />
    </svg>
  );
}

function ChevronRight() {
  return (
    <svg
      className="h-4 w-4 shrink-0 text-neutral-500"
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      aria-hidden="true"
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M8 6l4 4-4 4" />
    </svg>
  );
}

function SwapIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M7 4L3 8l4 4M3 8h13M17 20l4-4-4-4M21 16H8"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function ThinkingGlyph() {
  // A small "spark" mark for the thinking row.
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M12 3l1.8 4.7L18.5 9.5 13.8 11.3 12 16l-1.8-4.7L5.5 9.5l4.7-1.8L12 3z"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M5 13l4 4L19 7"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// Ascending bars — the «Усилие» (effort-level) glyph. Gives that row a leading
// icon so its label aligns with the icon-bearing rows above and below it.
function EffortIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M6 20v-5M12 20V8M18 20v-9"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  );
}

// The composer's model control. Clicking the model name opens a native-dark
// context menu: change model, toggle extended thinking (Apple switch), and pick
// an effort level (nested submenu). Replaces the old direct model-picker open.
export function ModelMenu({
  selectedModel,
  disabled,
  thinkingEnabled,
  thinkingEffort,
  toolsEnabled,
  onOpenModelSelector,
  onToggleThinking,
  onSetThinkingEffort,
  onToggleTools,
  modelSelectorOpen = false,
}: ModelMenuProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [effortOpen, setEffortOpen] = useState(false);
  // a11y (tr-34yj): the model trigger button. The picker opens from "Сменить
  // модель" (which closes this menu, so its item can't hold focus); when the
  // picker later closes, return focus here instead of dropping to <body>.
  const triggerRef = useRef<HTMLButtonElement>(null);
  const wasSelectorOpen = useRef(false);
  useEffect(() => {
    if (wasSelectorOpen.current && !modelSelectorOpen) {
      triggerRef.current?.focus();
    }
    wasSelectorOpen.current = modelSelectorOpen;
  }, [modelSelectorOpen]);

  const supportsReasoning = selectedModel?.supports_reasoning ?? false;
  const supportsTools = selectedModel?.supports_tools ?? false;
  const effectiveTools = toolsEnabled && supportsTools;
  // Thinking only applies on a supporting model; reflect that in the controls
  // even if the toggle was left on before switching to an unsupported model.
  const effectiveThinking = thinkingEnabled && supportsReasoning;
  const tier = selectedModel
    ? getTierInfo(priceNum(selectedModel.input_price_per_m))
    : null;

  const closeMenu = () => {
    setMenuOpen(false);
    setEffortOpen(false);
  };

  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeMenu();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  return (
    <div className="relative">
      {/* max-w-full caps the trigger at its wrapper width (a <button> otherwise
          sizes to content and overflows it); the name's min-w-0 + truncate then
          ellipsizes a long model name instead of colliding with the next button. */}
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setMenuOpen((v) => !v)}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        title={selectedModel?.name}
        className="flex min-w-0 max-w-full items-center gap-1.5 rounded-full px-2.5 py-1.5 text-sm text-neutral-300 transition-colors hover:bg-neutral-700/50 hover:text-neutral-100 disabled:opacity-50"
      >
        {selectedModel && tier ? (
          <>
            <span className="relative inline-flex shrink-0">
              <ModelTierIcon
                inputPricePerMillion={priceNum(selectedModel.input_price_per_m)}
                size={16}
              />
              {/* Extended-thinking indicator as a corner badge: absolutely
                  positioned so it carries ZERO layout width. Toggling thinking
                  never resizes the trigger (no left-shift), and the button still
                  hugs the model name snugly (no reserved-gap dead space). */}
              {effectiveThinking ? (
                <span
                  className="absolute -right-1 -top-1 h-2 w-2 rounded-full bg-emerald-500 shadow-[0_0_5px_1px_rgba(16,185,129,0.7)] ring-2 ring-[var(--trinity-input)]"
                  title="Расширенное мышление включено"
                  aria-hidden="true"
                />
              ) : null}
            </span>
            {/* min-w-0 lets this flex child shrink below its content so `truncate`
                actually engages — without it a long name overflows the trigger and
                collides with the analytics button to the right. */}
            <span className="min-w-0 truncate">{shortModelName(selectedModel.name)}</span>
          </>
        ) : (
          <span className="text-neutral-500">Выберите модель</span>
        )}
        <ChevronDown />
      </button>

      {menuOpen ? (
        <>
          {/* Click-catcher closes the menu without stealing the next click's target. */}
          <button
            type="button"
            aria-hidden="true"
            tabIndex={-1}
            onClick={closeMenu}
            className="fixed inset-0 z-30 cursor-default"
          />
          <div
            role="menu"
            className="absolute bottom-full left-0 z-40 mb-2 w-64 animate-scaleIn rounded-xl border border-neutral-700 bg-neutral-800 p-1.5 shadow-2xl"
          >
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                closeMenu();
                onOpenModelSelector();
              }}
              className="flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left text-sm text-neutral-200 transition-colors hover:bg-neutral-700"
            >
              <span className="shrink-0 text-neutral-400">
                <SwapIcon />
              </span>
              <span className="flex-1">Сменить модель</span>
            </button>

            <div className="my-1 h-px bg-neutral-700/70" />

            {/* Thinking — the WHOLE row toggles (not just the switch). Disabled +
                tooltip when the model can't reason. */}
            <button
              type="button"
              role="menuitemcheckbox"
              aria-checked={effectiveThinking}
              disabled={!supportsReasoning}
              onClick={onToggleThinking}
              title={
                supportsReasoning
                  ? "Модель строит цепочку рассуждений перед ответом"
                  : "Эта модель не поддерживает расширенное мышление"
              }
              className={`flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left text-sm transition-colors disabled:cursor-not-allowed disabled:hover:bg-transparent ${
                supportsReasoning
                  ? "text-neutral-200 hover:bg-neutral-700"
                  : "text-neutral-500"
              }`}
            >
              <span
                className={`shrink-0 ${
                  effectiveThinking ? "text-emerald-500" : "text-neutral-400"
                }`}
              >
                <ThinkingGlyph />
              </span>
              <span className="flex-1">Thinking</span>
              <ToggleVisual checked={effectiveThinking} disabled={!supportsReasoning} />
            </button>

            {/* Effort row → nested submenu. Active only when thinking is on. The
                leading EffortIcon aligns its label with the rows above/below. */}
            <div className="relative">
              <button
                type="button"
                role="menuitem"
                disabled={!effectiveThinking}
                aria-haspopup="menu"
                aria-expanded={effortOpen}
                onClick={() => setEffortOpen((v) => !v)}
                className="flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left text-sm text-neutral-200 transition-colors hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
              >
                <span className="shrink-0 text-neutral-400">
                  <EffortIcon />
                </span>
                <span className="flex-1">Усилие</span>
                <span className="text-xs text-neutral-500">
                  {EFFORT_LABEL[thinkingEffort]}
                </span>
                <ChevronRight />
              </button>

              {effortOpen && effectiveThinking ? (
                <div
                  role="menu"
                  className="absolute bottom-0 left-full z-50 ml-1 w-40 animate-scaleIn rounded-xl border border-neutral-700 bg-neutral-800 p-1.5 shadow-2xl"
                >
                  {EFFORT_OPTIONS.map((opt) => (
                    <button
                      key={opt.value}
                      type="button"
                      role="menuitemradio"
                      aria-checked={thinkingEffort === opt.value}
                      onClick={() => {
                        onSetThinkingEffort(opt.value);
                        setEffortOpen(false);
                      }}
                      className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-sm text-neutral-200 transition-colors hover:bg-neutral-700"
                    >
                      <span className="flex w-4 shrink-0 justify-center text-emerald-500">
                        {thinkingEffort === opt.value ? <CheckIcon /> : null}
                      </span>
                      <span className="flex-1">{opt.label}</span>
                    </button>
                  ))}
                </div>
              ) : null}
            </div>

            {/* Divider + Инструменты (agentic tool-use) — whole row toggles,
                gated by the model's supports_tools. */}
            <div className="my-1 h-px bg-neutral-700/70" />
            <button
              type="button"
              role="menuitemcheckbox"
              aria-checked={effectiveTools}
              disabled={!supportsTools}
              onClick={onToggleTools}
              title={
                supportsTools
                  ? "Модель может создавать и править файлы инструментами"
                  : "Эта модель не поддерживает инструменты"
              }
              className={`flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left text-sm transition-colors disabled:cursor-not-allowed disabled:hover:bg-transparent ${
                supportsTools
                  ? "text-neutral-200 hover:bg-neutral-700"
                  : "text-neutral-500"
              }`}
            >
              <span
                className={`shrink-0 ${
                  effectiveTools ? "text-emerald-500" : "text-neutral-400"
                }`}
              >
                <ToolsGlyph />
              </span>
              <span className="flex-1">Инструменты</span>
              <ToggleVisual checked={effectiveTools} disabled={!supportsTools} />
            </button>
          </div>
        </>
      ) : null}
    </div>
  );
}

function ToolsGlyph() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M14.7 6.3a4 4 0 0 0-5.4 5.2L4 16.8 7.2 20l5.3-5.3a4 4 0 0 0 5.2-5.4l-2.5 2.5-2.3-.6-.6-2.3 2.4-2.6z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export default ModelMenu;
