"use client";

import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";

export interface LightboxProps {
  // Full-resolution image sources (data URIs or object URLs).
  images: string[];
  // Index of the image to open at; null = closed.
  index: number | null;
  onClose: () => void;
}

// Full-screen image viewer rendered into document.body via a portal so it sits
// above the whole app. Closes on backdrop click, Escape, or the ✕ button;
// ←/→ (keys or on-screen arrows) navigate when the message has several images.
export function Lightbox({ images, index, onClose }: LightboxProps) {
  const [current, setCurrent] = useState(index ?? 0);
  const [mounted, setMounted] = useState(false);

  // Portals need a DOM target — only render after mount (SSR-safe).
  useEffect(() => setMounted(true), []);

  // Sync internal cursor whenever the opener changes the starting index.
  useEffect(() => {
    if (index !== null) setCurrent(index);
  }, [index]);

  const isOpen = index !== null;
  const count = images.length;

  const goPrev = useCallback(() => {
    setCurrent((c) => (c - 1 + count) % count);
  }, [count]);
  const goNext = useCallback(() => {
    setCurrent((c) => (c + 1) % count);
  }, [count]);

  // Keyboard: Escape closes, arrows navigate. Bound only while open.
  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowLeft" && count > 1) goPrev();
      else if (e.key === "ArrowRight" && count > 1) goNext();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isOpen, count, onClose, goPrev, goNext]);

  // Lock body scroll while the overlay is open.
  useEffect(() => {
    if (!isOpen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [isOpen]);

  if (!mounted || !isOpen || count === 0) return null;

  const src = images[Math.min(current, count - 1)];

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Просмотр изображения"
      onClick={onClose}
      className="fixed inset-0 z-[100] flex animate-fadeIn items-center justify-center bg-black/80 backdrop-blur-sm"
    >
      {/* Close button */}
      <button
        type="button"
        onClick={onClose}
        aria-label="Закрыть"
        title="Закрыть"
        className="absolute right-4 top-4 flex h-10 w-10 items-center justify-center rounded-full bg-white/10 text-white transition-colors hover:bg-white/20"
      >
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
      </button>

      {count > 1 ? (
        <>
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              goPrev();
            }}
            aria-label="Предыдущее изображение"
            className="absolute left-4 flex h-11 w-11 items-center justify-center rounded-full bg-white/10 text-white transition-colors hover:bg-white/20"
          >
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M15 18l-6-6 6-6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              goNext();
            }}
            aria-label="Следующее изображение"
            className="absolute right-4 flex h-11 w-11 items-center justify-center rounded-full bg-white/10 text-white transition-colors hover:bg-white/20"
          >
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M9 18l6-6-6-6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
          <span className="absolute bottom-4 left-1/2 -translate-x-1/2 rounded-full bg-white/10 px-3 py-1 text-xs text-white">
            {current + 1} / {count}
          </span>
        </>
      ) : null}

      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        key={src}
        src={src}
        alt="Изображение"
        onClick={(e) => e.stopPropagation()}
        className="max-h-[90vh] max-w-[90vw] animate-scaleIn rounded-lg object-contain shadow-2xl"
      />
    </div>,
    document.body,
  );
}

export default Lightbox;
