import { describe, it, expect, vi, afterEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { act } from "react";
import { useThinkingClock } from "./useThinkingClock";

afterEach(() => vi.useRealTimers());

describe("useThinkingClock", () => {
  it("returns the frozen duration when inactive", () => {
    const { result } = renderHook(() =>
      useThinkingClock(false, undefined, 6000),
    );
    expect(result.current).toBe(6000);
  });

  it("returns 0 when inactive with no duration", () => {
    const { result } = renderHook(() =>
      useThinkingClock(false, undefined, undefined),
    );
    expect(result.current).toBe(0);
  });

  it("advances while active", () => {
    vi.useFakeTimers();
    const start = Date.now();
    const { result } = renderHook(() => useThinkingClock(true, start, undefined));
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBeGreaterThanOrEqual(1000);
  });

  it("freezes to the duration when it stops being active", () => {
    vi.useFakeTimers();
    const start = Date.now();
    const { result, rerender } = renderHook(
      ({ active, dur }: { active: boolean; dur?: number }) =>
        useThinkingClock(active, start, dur),
      { initialProps: { active: true, dur: undefined as number | undefined } },
    );
    act(() => {
      vi.advanceTimersByTime(500);
    });
    rerender({ active: false, dur: 5000 });
    expect(result.current).toBe(5000);
  });
});
