"use client";

import { useEffect, useRef, useState } from "react";

import { formatBalance } from "@/lib/formatting";
import { api_string_to_micro, micro_to_cost_string } from "@/lib/microRub";

const DURATION_MS = 600;

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

// Animated balance readout (tr-hye6). When the balance changes — a top-up
// landing, a chat-send debit — the number counts from the old value to the new
// over a short ease-out, and an INCREASE briefly flashes emerald (money in).
// formatBalance is reused per frame so every intermediate value renders in the
// exact final format. Honors prefers-reduced-motion (snaps, no animation). The
// first value (mount) never animates.
export function AnimatedBalance({ apiString }: { apiString: string }) {
  const target = Number(api_string_to_micro(apiString));
  const [display, setDisplay] = useState(target);
  const [rising, setRising] = useState(false);
  const prevTarget = useRef(target);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    const from = prevTarget.current;
    const to = target;
    prevTarget.current = to;
    if (from === to) return;

    if (prefersReducedMotion()) {
      setDisplay(to);
      return;
    }

    if (to > from) setRising(true);
    const start = performance.now();
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / DURATION_MS);
      const eased = 1 - Math.pow(1 - t, 3); // ease-out cubic
      setDisplay(Math.round(from + (to - from) * eased));
      if (t < 1) {
        rafRef.current = requestAnimationFrame(step);
      } else {
        setDisplay(to);
      }
    };
    rafRef.current = requestAnimationFrame(step);
    const flashOff = setTimeout(() => setRising(false), DURATION_MS + 200);

    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      clearTimeout(flashOff);
    };
  }, [target]);

  return (
    <span
      className={`inline-block tabular-nums transition-[color,transform] duration-300 ease-out ${
        rising ? "scale-105 text-emerald-400" : "scale-100"
      }`}
    >
      {formatBalance(micro_to_cost_string(display))}
    </span>
  );
}
