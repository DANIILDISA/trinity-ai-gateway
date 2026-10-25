"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

export type ToastVariant = "success" | "error" | "info";

export interface ToastInput {
  message: string;
  variant?: ToastVariant;
  durationMs?: number;
}

interface ToastEntry extends Required<Omit<ToastInput, "durationMs">> {
  id: number;
  durationMs: number;
}

interface ToastContextValue {
  show(input: ToastInput): void;
  success(message: string, durationMs?: number): void;
  error(message: string, durationMs?: number): void;
  info(message: string, durationMs?: number): void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

const DEFAULT_DURATION_MS = 4_000;
const ERROR_DURATION_MS = 6_000;

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastEntry[]>([]);
  const nextIdRef = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const show = useCallback((input: ToastInput) => {
    const variant = input.variant ?? "info";
    const id = nextIdRef.current++;
    const entry: ToastEntry = {
      id,
      message: input.message,
      variant,
      durationMs:
        input.durationMs ??
        (variant === "error" ? ERROR_DURATION_MS : DEFAULT_DURATION_MS),
    };
    setToasts((prev) => [...prev, entry]);
  }, []);

  const value = useMemo<ToastContextValue>(
    () => ({
      show,
      success: (message, durationMs) =>
        show({ message, variant: "success", durationMs }),
      error: (message, durationMs) =>
        show({ message, variant: "error", durationMs }),
      info: (message, durationMs) =>
        show({ message, variant: "info", durationMs }),
    }),
    [show],
  );

  return (
    <ToastContext.Provider value={value}>
      {children}
      <ToastViewport toasts={toasts} onDismiss={dismiss} />
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (ctx === null) {
    throw new Error("useToast must be used within a <ToastProvider>");
  }
  return ctx;
}

function ToastViewport({
  toasts,
  onDismiss,
}: {
  toasts: ToastEntry[];
  onDismiss: (id: number) => void;
}) {
  if (toasts.length === 0) return null;
  return (
    <div
      aria-live="polite"
      aria-atomic="false"
      className="pointer-events-none fixed inset-x-0 top-4 z-30 flex flex-col items-center gap-2 px-4"
    >
      {toasts.map((t) => (
        <ToastItem key={t.id} entry={t} onDismiss={onDismiss} />
      ))}
    </div>
  );
}

const VARIANT_CLASSES: Record<ToastVariant, string> = {
  success: "border-emerald-200 bg-emerald-50 text-emerald-900",
  error: "border-rose-200 bg-rose-50 text-rose-900",
  info: "border-neutral-200 bg-white text-neutral-900",
};

function ToastItem({
  entry,
  onDismiss,
}: {
  entry: ToastEntry;
  onDismiss: (id: number) => void;
}) {
  useEffect(() => {
    const id = setTimeout(() => onDismiss(entry.id), entry.durationMs);
    return () => clearTimeout(id);
  }, [entry.id, entry.durationMs, onDismiss]);

  const role = entry.variant === "error" ? "alert" : "status";

  return (
    <div
      role={role}
      className={`pointer-events-auto flex w-full max-w-md items-start justify-between gap-3 rounded border px-4 py-2 text-sm shadow-sm ${VARIANT_CLASSES[entry.variant]}`}
    >
      <span className="flex-1">{entry.message}</span>
      <button
        type="button"
        onClick={() => onDismiss(entry.id)}
        aria-label="Закрыть"
        className="text-xs uppercase tracking-wide opacity-70 hover:opacity-100"
      >
        ✕
      </button>
    </div>
  );
}

export default ToastProvider;
