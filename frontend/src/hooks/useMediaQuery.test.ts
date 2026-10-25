import { describe, it, expect, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";

import { useMediaQuery } from "./useMediaQuery";

function stubMatchMedia(initial: boolean) {
  let listener: ((e: { matches: boolean }) => void) | null = null;
  const mql = {
    matches: initial,
    media: "",
    onchange: null,
    addEventListener: (_: string, l: (e: { matches: boolean }) => void) => {
      listener = l;
    },
    removeEventListener: () => {
      listener = null;
    },
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  };
  window.matchMedia = vi.fn().mockReturnValue(mql) as unknown as typeof window.matchMedia;
  return {
    emit: (matches: boolean) => {
      mql.matches = matches;
      if (listener) act(() => listener!({ matches }));
    },
  };
}

describe("useMediaQuery", () => {
  it("returns the initial match state", () => {
    stubMatchMedia(true);
    const { result } = renderHook(() => useMediaQuery("(min-width: 1024px)"));
    expect(result.current).toBe(true);
  });

  it("updates when the media query changes", () => {
    const ctl = stubMatchMedia(false);
    const { result } = renderHook(() => useMediaQuery("(min-width: 1024px)"));
    expect(result.current).toBe(false);
    ctl.emit(true);
    expect(result.current).toBe(true);
  });
});
