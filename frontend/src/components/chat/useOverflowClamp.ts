"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";

// Measures whether content exceeds `clampPx` and tracks an expand/collapse
// toggle. While `disabled` (e.g. a thinking stream is still active) it never
// clamps and reports no overflow. Shared by the thinking body and large user
// messages for the Show more/less behaviour.
export function useOverflowClamp(clampPx: number, disabled = false) {
  const ref = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);

  // Re-measure after every render so streamed-in content updates the state.
  // scrollHeight reports full content height even while clamped; a no-op
  // setState (same value) won't re-render, so this can't loop.
  useLayoutEffect(() => {
    const el = ref.current;
    if (el) setOverflowing(el.scrollHeight > clampPx + 8);
  });

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      setOverflowing(el.scrollHeight > clampPx + 8);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [clampPx]);

  const effectiveOverflow = !disabled && overflowing;
  return {
    ref,
    overflowing: effectiveOverflow,
    expanded,
    clamped: effectiveOverflow && !expanded,
    toggle: () => setExpanded((v) => !v),
  };
}
