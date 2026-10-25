"use client";

import { useEffect, useState } from "react";

// While active, ticks ~4×/s so the header can show a live "Размышляю… Nс"
// counter anchored at `startedAt`. When inactive, returns the frozen
// `durationMs` (or 0). The ticking lives here, not in message state, so the
// memoised MessageBubble doesn't re-render every tick.
export function useThinkingClock(
  isActive: boolean,
  startedAt?: number,
  durationMs?: number,
): number {
  const [now, setNow] = useState<number | null>(null);

  useEffect(() => {
    if (!isActive) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, [isActive]);

  if (!isActive) return durationMs ?? 0;
  if (startedAt == null) return 0;
  return Math.max(0, (now ?? Date.now()) - startedAt);
}
