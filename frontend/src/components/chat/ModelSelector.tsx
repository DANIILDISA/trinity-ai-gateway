"use client";

import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { ModelCapabilityBadges } from "@/components/ui/ModelCapabilityBadges";
import { ModelTierIcon, getTierInfo } from "@/components/ui/ModelTierIcon";
import { apiClient } from "@/lib/api";
import { formatContextWindow } from "@/lib/formatting";
import type { ModelInfo, ModelListResponse } from "@/types/api";

type SortMode = "name" | "price-asc" | "price-desc";

function rub(value: number): string {
  return Math.round(Number.isFinite(value) ? value : 0).toLocaleString("ru-RU");
}

export interface ModelSelectorProps {
  models: ModelInfo[];
  selectedModelId: string;
  disabled: boolean;
  onChange: (modelId: string) => void;
  // When true the built-in trigger button is not rendered — the modal is
  // controlled entirely via `open`/`onOpenChange` (used by the composer, which
  // renders its own model-name chevron).
  hideTrigger?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

function priceNum(s: string | undefined): number {
  const n = Number.parseFloat(s ?? "");
  return Number.isFinite(n) ? n : 0;
}

// A price is "bad" when it is missing/empty, unparseable, or negative — these
// come from dynamic-price routers. Such models can't show a real price.
function priceIsBad(s: string | undefined | null): boolean {
  if (s == null || s.trim() === "") return true;
  const n = Number.parseFloat(s);
  return !Number.isFinite(n) || n < 0;
}

function hasBadPrice(m: ModelInfo): boolean {
  return priceIsBad(m.input_price_per_m) || priceIsBad(m.output_price_per_m);
}

type ProviderKey = "google" | "openai" | "deepseek" | "anthropic" | "qwen";

const PROVIDERS: { key: ProviderKey; label: string; icon: string }[] = [
  { key: "google", label: "Google", icon: "🔵" },
  { key: "openai", label: "OpenAI", icon: "⚫" },
  { key: "deepseek", label: "DeepSeek", icon: "🔷" },
  { key: "anthropic", label: "Anthropic", icon: "🟠" },
  { key: "qwen", label: "Qwen", icon: "🟣" },
];

function matchesProvider(m: ModelInfo, key: ProviderKey): boolean {
  const provider = m.provider.toLowerCase();
  // Qwen also matches on display name, since some routers don't carry it in
  // the provider field.
  if (key === "qwen") {
    return provider.includes("qwen") || m.name.toLowerCase().includes("qwen");
  }
  return provider.includes(key);
}

// Module-level cache shared across instances. Cleared on full page reload only.
let modelsCache: ModelInfo[] | null = null;
let modelsPromise: Promise<ModelInfo[]> | null = null;

// Persisted UI state for the selector. Lives at module scope so the chosen
// sort / filters / search and the scroll position survive close→reopen cycles
// (and even a component remount) within a page session. Reset only on full page
// reload — matching `modelsCache` above. Bug 1.
let persistedQuery = "";
let persistedSort: SortMode = "name";
let persistedProvider: ProviderKey | null = null;
let persistedFileFilter = false;
let persistedImageFilter = false;
let persistedReasoningFilter = false;
let persistedWebSearchFilter = false;
// Index of the topmost visible model row, captured on close and restored on
// open (item-level, not pixel-level). Bug 1.
let persistedScrollIndex = 0;

// Topmost visible row index inside the scrollable list. Uses layout offsets
// (offsetTop), so it is unaffected by the modal's open/close scale transform.
function topmostVisibleIndex(list: HTMLElement): number {
  const first = list.firstElementChild as HTMLElement | null;
  if (!first) return 0;
  const base = first.offsetTop;
  const scroll = list.scrollTop;
  for (let i = 0; i < list.children.length; i += 1) {
    const child = list.children[i] as HTMLElement;
    const bottom = child.offsetTop - base + child.offsetHeight;
    if (bottom > scroll + 1) return i;
  }
  return 0;
}

export async function loadModels(force = false): Promise<ModelInfo[]> {
  if (!force && modelsCache) return modelsCache;
  if (!force && modelsPromise) return modelsPromise;
  modelsPromise = apiClient
    .get<ModelListResponse>("/api/v1/models")
    .then((res) => {
      modelsCache = res.models ?? [];
      modelsPromise = null;
      return modelsCache;
    })
    .catch((err) => {
      modelsPromise = null;
      throw err;
    });
  return modelsPromise;
}

export function ModelSelector({
  models,
  selectedModelId,
  disabled,
  onChange,
  hideTrigger = false,
  open,
  onOpenChange,
}: ModelSelectorProps) {
  const [mounted, setMounted] = useState(false);
  const [visible, setVisible] = useState(false);
  // UI state seeded from the module-level persisted values (Bug 1).
  const [query, setQuery] = useState(persistedQuery);
  const [sort, setSort] = useState<SortMode>(persistedSort);
  // Capability filters (independent of sort). When several are on, AND semantics
  // apply — only models supporting all selected capabilities survive.
  const [fileFilter, setFileFilter] = useState(persistedFileFilter);
  const [imageFilter, setImageFilter] = useState(persistedImageFilter);
  const [reasoningFilter, setReasoningFilter] = useState(persistedReasoningFilter);
  const [webSearchFilter, setWebSearchFilter] = useState(persistedWebSearchFilter);
  const [activeProvider, setActiveProvider] = useState<ProviderKey | null>(
    persistedProvider,
  );
  const [activeIndex, setActiveIndex] = useState(0);

  const searchRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLUListElement | null>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // a11y (tr-34yj): the element focused when the modal opened, so focus can
  // return to it on close instead of dropping to <body> (WCAG 2.4.3).
  const triggerRef = useRef<HTMLElement | null>(null);
  // True only for keyboard-driven active-index changes, so the scroll-into-view
  // effect doesn't fire on mouse hover and fight the user's scroll (Bug 2).
  const navScrollRef = useRef(false);

  // Mirror UI state back into the module-level store so it survives reopen.
  // These writes don't re-render the model list. Bug 1.
  useEffect(() => {
    persistedQuery = query;
  }, [query]);
  useEffect(() => {
    persistedSort = sort;
  }, [sort]);
  useEffect(() => {
    persistedProvider = activeProvider;
  }, [activeProvider]);
  useEffect(() => {
    persistedFileFilter = fileFilter;
  }, [fileFilter]);
  useEffect(() => {
    persistedImageFilter = imageFilter;
  }, [imageFilter]);
  useEffect(() => {
    persistedReasoningFilter = reasoningFilter;
  }, [reasoningFilter]);
  useEffect(() => {
    persistedWebSearchFilter = webSearchFilter;
  }, [webSearchFilter]);

  const isDisabled = disabled || models.length === 0;
  const selected = models.find((m) => m.id === selectedModelId);
  const selectedTier = selected
    ? getTierInfo(priceNum(selected.input_price_per_m))
    : null;

  // Search filter only — shared by the provider chips so they can tell which
  // would yield results for the current query.
  const searchFiltered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q
      ? models.filter((m) => m.name.toLowerCase().includes(q))
      : models.slice();
  }, [models, query]);

  const providerHasResults = useCallback(
    (key: ProviderKey) => searchFiltered.some((m) => matchesProvider(m, key)),
    [searchFiltered],
  );

  // Apply the provider filter (after search), then sort by the chosen mode.
  // Recomputed only when inputs change so keyboard nav indexes into a stable
  // array.
  const visibleModels = useMemo(() => {
    let filtered = activeProvider
      ? searchFiltered.filter((m) => matchesProvider(m, activeProvider))
      : searchFiltered.slice();
    // Capability filters compose with the provider filter and the sort below.
    if (imageFilter) filtered = filtered.filter((m) => m.supports_image_input);
    if (fileFilter) filtered = filtered.filter((m) => m.supports_file_input);
    if (reasoningFilter)
      filtered = filtered.filter((m) => m.supports_reasoning);
    if (webSearchFilter)
      filtered = filtered.filter((m) => m.supports_web_search);
    filtered.sort((a, b) => {
      // Models without a real price always sink to the bottom, whatever the
      // chosen sort.
      const aBad = hasBadPrice(a);
      const bBad = hasBadPrice(b);
      if (aBad !== bBad) return aBad ? 1 : -1;
      if (sort === "name") return a.name.localeCompare(b.name, "ru");
      const pa = priceNum(a.input_price_per_m);
      const pb = priceNum(b.input_price_per_m);
      return sort === "price-asc" ? pa - pb : pb - pa;
    });
    return filtered;
  }, [
    searchFiltered,
    activeProvider,
    sort,
    fileFilter,
    imageFilter,
    reasoningFilter,
    webSearchFilter,
  ]);

  // Three-state price toggle: off → ascending → descending → off (back to the
  // name sort). The active direction is surfaced as an arrow on the button.
  const cyclePriceSort = useCallback(() => {
    setSort((s) =>
      s === "price-asc" ? "price-desc" : s === "price-desc" ? "name" : "price-asc",
    );
  }, []);

  const openModal = useCallback(() => {
    if (isDisabled) return;
    // a11y (tr-34yj): remember the trigger so focus returns to it on close.
    triggerRef.current = (document.activeElement as HTMLElement) ?? null;
    if (closeTimer.current) clearTimeout(closeTimer.current);
    setMounted(true);
    // Next frame: flip to visible so the enter transition runs.
    requestAnimationFrame(() => setVisible(true));
  }, [isDisabled]);

  const closeModal = useCallback(() => {
    // Capture the topmost visible row before the list unmounts so we can
    // restore the same visible area on reopen (Bug 1).
    if (listRef.current) {
      persistedScrollIndex = topmostVisibleIndex(listRef.current);
    }
    setVisible(false);
    closeTimer.current = setTimeout(() => setMounted(false), 200);
    onOpenChange?.(false);
    // a11y (tr-34yj): return focus to the trigger instead of dropping to <body>.
    triggerRef.current?.focus();
  }, [onOpenChange]);

  useEffect(() => {
    return () => {
      if (closeTimer.current) clearTimeout(closeTimer.current);
    };
  }, []);

  // Sync the externally-controlled `open` prop with the internal mount state.
  useEffect(() => {
    if (open === undefined) return;
    if (open && !mounted) openModal();
    else if (!open && mounted && visible) closeModal();
  }, [open, mounted, visible, openModal, closeModal]);

  // On open: keep the persisted search/sort/filters (Bug 1), seed the keyboard
  // active card on the current selection, and move focus to the search field.
  useEffect(() => {
    if (!mounted) return;
    const idx = visibleModels.findIndex((m) => m.id === selectedModelId);
    setActiveIndex(idx >= 0 ? idx : 0);
    const t = setTimeout(() => searchRef.current?.focus(), 0);
    return () => clearTimeout(t);
    // Only when the modal mounts.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mounted]);

  // On open: restore the saved scroll position (item-level). Runs before paint
  // and uses offsetTop math, so it's correct despite the open scale transform.
  // Bug 1.
  useLayoutEffect(() => {
    if (!mounted) return;
    const list = listRef.current;
    const first = list?.firstElementChild as HTMLElement | undefined;
    if (!list || !first) return;
    const target = list.children[persistedScrollIndex] as HTMLElement | undefined;
    list.scrollTop = target ? target.offsetTop - first.offsetTop : 0;
    // Only when the modal mounts.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mounted]);

  // Clamp active index when the filtered list shrinks.
  useEffect(() => {
    setActiveIndex((i) => Math.min(i, Math.max(visibleModels.length - 1, 0)));
  }, [visibleModels.length]);

  // Keep the active card scrolled into view during keyboard nav ONLY. Mouse
  // hover also moves the active index, but scrolling it into view on hover
  // fights the user's own scroll and causes stutter — so it's gated behind a
  // keyboard-only flag (Bug 2).
  useEffect(() => {
    if (!mounted) return;
    if (!navScrollRef.current) return;
    navScrollRef.current = false;
    const node = listRef.current?.children[activeIndex] as
      | HTMLElement
      | undefined;
    node?.scrollIntoView({ block: "nearest" });
  }, [mounted, activeIndex]);

  const commit = useCallback(
    (index: number) => {
      const m = visibleModels[index];
      if (m) {
        onChange(m.id);
        closeModal();
      }
    },
    [visibleModels, onChange, closeModal],
  );

  const handlePanelKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        navScrollRef.current = true;
        setActiveIndex((i) => Math.min(i + 1, visibleModels.length - 1));
        break;
      case "ArrowUp":
        e.preventDefault();
        navScrollRef.current = true;
        setActiveIndex((i) => Math.max(i - 1, 0));
        break;
      case "Home":
        e.preventDefault();
        navScrollRef.current = true;
        setActiveIndex(0);
        break;
      case "End":
        e.preventDefault();
        navScrollRef.current = true;
        setActiveIndex(visibleModels.length - 1);
        break;
      case "Enter":
        e.preventDefault();
        commit(activeIndex);
        break;
      case "Escape":
        e.preventDefault();
        closeModal();
        break;
    }
  };

  return (
    <div className="flex items-center gap-2 text-sm text-neutral-600">
      {hideTrigger ? null : (
        <>
          <span className="font-medium">Модель:</span>
          <button
            type="button"
            disabled={isDisabled}
            onClick={openModal}
            aria-haspopup="dialog"
            aria-expanded={mounted}
            className="flex h-9 min-w-[14rem] items-center gap-2 rounded border border-neutral-700 bg-neutral-900 px-2 text-left text-sm text-neutral-100 hover:border-neutral-600 hover:bg-neutral-800 disabled:bg-neutral-800 disabled:text-neutral-500"
          >
            {models.length === 0 ? (
              <span className="text-neutral-500">Загрузка...</span>
            ) : selected && selectedTier ? (
              <>
                <ModelTierIcon
                  inputPricePerMillion={priceNum(selected.input_price_per_m)}
                  size={18}
                />
                <span className="truncate">{selected.name}</span>
                <span className="text-neutral-400">•</span>
                <span
                  className="shrink-0 font-medium"
                  style={{ color: selectedTier.color }}
                >
                  {selectedTier.name}
                </span>
              </>
            ) : (
              <span className="text-neutral-500">Выберите модель</span>
            )}
            <svg
              className="ml-auto h-4 w-4 shrink-0 text-neutral-400"
              viewBox="0 0 20 20"
              fill="none"
              stroke="currentColor"
              strokeWidth={1.5}
              aria-hidden="true"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4 4-4" />
            </svg>
          </button>
        </>
      )}

      {mounted ? (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Выбор модели"
          onKeyDown={handlePanelKeyDown}
          className={`fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm transition-opacity duration-200 ${
            visible ? "opacity-100" : "opacity-0"
          }`}
          onClick={(e) => {
            if (e.target === e.currentTarget) closeModal();
          }}
        >
          <div
            className={`flex h-[80vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-neutral-800 bg-neutral-900 text-neutral-100 shadow-2xl transition-all duration-200 ${
              visible ? "scale-100 opacity-100" : "scale-95 opacity-0"
            }`}
          >
            <div className="flex items-center justify-between border-b border-neutral-800 px-5 py-4">
              <h2 className="text-base font-semibold">Выбор модели</h2>
              <button
                type="button"
                onClick={closeModal}
                aria-label="Закрыть"
                className="rounded p-1 text-neutral-400 hover:bg-neutral-800 hover:text-neutral-100"
              >
                <svg
                  className="h-5 w-5"
                  viewBox="0 0 20 20"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={1.5}
                  aria-hidden="true"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M6 6l8 8M14 6l-8 8"
                  />
                </svg>
              </button>
            </div>

            <div className="flex flex-col gap-3 border-b border-neutral-800 px-5 py-4">
              <input
                ref={searchRef}
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Поиск модели..."
                className="h-9 w-full rounded-md border border-neutral-700 bg-neutral-800 px-3 text-sm text-neutral-100 placeholder:text-neutral-500 outline-none focus:border-emerald-600"
              />
              <div className="flex flex-wrap gap-2">
                <SortPill
                  active={sort === "price-asc" || sort === "price-desc"}
                  onClick={cyclePriceSort}
                >
                  По цене
                  {sort === "price-asc"
                    ? " ↑"
                    : sort === "price-desc"
                      ? " ↓"
                      : ""}
                </SortPill>
                <SortPill active={sort === "name"} onClick={() => setSort("name")}>
                  По имени
                </SortPill>
                <SortPill
                  active={fileFilter}
                  onClick={() => setFileFilter((v) => !v)}
                >
                  Файлы
                </SortPill>
                <SortPill
                  active={imageFilter}
                  onClick={() => setImageFilter((v) => !v)}
                >
                  Анализ фото
                </SortPill>
                <SortPill
                  active={reasoningFilter}
                  onClick={() => setReasoningFilter((v) => !v)}
                >
                  Thinking
                </SortPill>
                <SortPill
                  active={webSearchFilter}
                  onClick={() => setWebSearchFilter((v) => !v)}
                >
                  Веб-поиск
                </SortPill>
              </div>
            </div>

            <div className="border-b border-neutral-800 px-5 py-3">
              <div className="flex w-full flex-wrap gap-2">
                <ProviderChip
                  active={activeProvider === null}
                  neutralActive
                  onClick={() => setActiveProvider(null)}
                >
                  Все
                </ProviderChip>
                {PROVIDERS.map((p) => (
                  <ProviderChip
                    key={p.key}
                    active={activeProvider === p.key}
                    dimmed={!providerHasResults(p.key)}
                    onClick={() =>
                      setActiveProvider((cur) => (cur === p.key ? null : p.key))
                    }
                  >
                    <span aria-hidden="true">{p.icon}</span>
                    {p.label}
                  </ProviderChip>
                ))}
              </div>
            </div>

            <ul
              ref={listRef}
              role="listbox"
              aria-label="Модели"
              className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-5 py-4"
            >
              {visibleModels.length === 0 ? (
                <li className="py-8 text-center text-sm text-neutral-500">
                  Модели не найдены.
                </li>
              ) : (
                visibleModels.map((m, i) => (
                  <ModelRow
                    key={m.id}
                    model={m}
                    index={i}
                    isSelected={m.id === selectedModelId}
                    isActive={i === activeIndex}
                    onSelect={commit}
                    onHover={setActiveIndex}
                  />
                ))
              )}
            </ul>
          </div>
        </div>
      ) : null}
    </div>
  );
}

interface ModelRowProps {
  model: ModelInfo;
  index: number;
  isSelected: boolean;
  isActive: boolean;
  onSelect: (index: number) => void;
  onHover: (index: number) => void;
}

// Memoized row. With stable `onSelect`/`onHover` callbacks, only the rows whose
// `isSelected`/`isActive` actually change re-render — so hovering while
// fast-scrolling no longer repaints the whole list (Bug 2).
const ModelRow = memo(function ModelRow({
  model: m,
  index,
  isSelected,
  isActive,
  onSelect,
  onHover,
}: ModelRowProps) {
  const tier = getTierInfo(priceNum(m.input_price_per_m));
  const highlight = isSelected || isActive;
  return (
    <li
      role="option"
      aria-selected={isSelected}
      onClick={() => onSelect(index)}
      onMouseEnter={() => onHover(index)}
      style={{
        borderColor: highlight ? tier.color : undefined,
        backgroundColor: isSelected ? `${tier.color}14` : undefined,
      }}
      className={`flex cursor-pointer items-start gap-3 rounded-lg border bg-neutral-800 p-3 transition-colors ${
        highlight ? "" : "border-neutral-700 hover:border-neutral-600"
      }`}
    >
      <div className="mt-0.5 shrink-0">
        <ModelTierIcon
          inputPricePerMillion={priceNum(m.input_price_per_m)}
          size={24}
        />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate font-semibold text-neutral-100">
            {m.name}
          </span>
          <span
            className="shrink-0 rounded-full px-2 py-0.5 text-xs font-medium"
            style={{
              color: tier.color,
              backgroundColor: `${tier.color}1f`,
            }}
          >
            {tier.name}
          </span>
          {m.status !== "available" ? (
            <span className="shrink-0 text-xs text-neutral-500">
              недоступна
            </span>
          ) : null}
        </div>
        <div className="truncate text-xs text-neutral-500">{m.provider}</div>
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-neutral-300">
          {hasBadPrice(m) ? (
            <span className="italic text-neutral-500">💰 Цена по запросу</span>
          ) : (
            <span>
              💰 Вход: {rub(priceNum(m.input_price_per_m))} ₽/1М
              {"  |  "}
              Выход: {rub(priceNum(m.output_price_per_m))} ₽/1М
            </span>
          )}
          <span>📐 Контекст: {formatContextWindow(m.context_window)}</span>
        </div>
        <div className="mt-1.5">
          <ModelCapabilityBadges
            supportsImageInput={m.supports_image_input ?? false}
            supportsFileInput={m.supports_file_input ?? false}
            supportsTools={m.supports_tools ?? false}
            supportsReasoning={m.supports_reasoning ?? false}
            supportsWebSearch={m.supports_web_search ?? false}
          />
        </div>
      </div>
    </li>
  );
});

function SortPill({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
        active
          ? "border-neutral-600 bg-neutral-700 text-neutral-100"
          : "border-neutral-700 bg-neutral-800 text-neutral-400 hover:bg-neutral-700"
      }`}
    >
      {children}
    </button>
  );
}

function ProviderChip({
  active,
  dimmed,
  neutralActive,
  onClick,
  children,
}: {
  active: boolean;
  dimmed?: boolean;
  neutralActive?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  const activeClass = neutralActive
    ? "border-neutral-600 bg-neutral-700 text-neutral-100"
    : "border-emerald-600 bg-emerald-600/20 text-emerald-400";
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
        active ? activeClass : "border-neutral-700 bg-neutral-800 text-neutral-400 hover:bg-neutral-700"
      } ${dimmed ? "opacity-40" : ""}`}
    >
      {children}
    </button>
  );
}

// Convenience hook for callers that want the cached list with a simple
// loading boolean.
export function useModels(): { models: ModelInfo[]; isLoading: boolean } {
  const [models, setModels] = useState<ModelInfo[]>(modelsCache ?? []);
  const [isLoading, setIsLoading] = useState(modelsCache === null);

  useEffect(() => {
    let cancelled = false;
    loadModels()
      .then((list) => {
        if (!cancelled) setModels(list);
      })
      .catch(() => {
        /* leave empty */
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return { models, isLoading };
}

export default ModelSelector;
