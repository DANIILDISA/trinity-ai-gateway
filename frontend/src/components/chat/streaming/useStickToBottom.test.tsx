import { act, renderHook } from "@testing-library/react";
import { createRef } from "react";
import { describe, expect, it } from "vitest";

import { useStickToBottom } from "./useStickToBottom";

function makeList() {
  const el = document.createElement("div");
  Object.defineProperty(el, "clientHeight", { value: 100, configurable: true });
  Object.defineProperty(el, "scrollHeight", { value: 500, configurable: true });
  let top = 400; // at bottom (500 - 100)
  Object.defineProperty(el, "scrollTop", {
    get: () => top,
    set: (v) => {
      top = v;
    },
    configurable: true,
  });
  document.body.appendChild(el);
  return el;
}

describe("useStickToBottom", () => {
  it("starts pinned and detaches on wheel-up", () => {
    const el = makeList();
    const ref = createRef<HTMLDivElement>();
    (ref as { current: HTMLDivElement }).current = el;
    const { result } = renderHook(() => useStickToBottom(ref));
    expect(result.current.isPinned).toBe(true);
    act(() => {
      el.dispatchEvent(Object.assign(new Event("wheel"), { deltaY: -50 }));
    });
    expect(result.current.isPinned).toBe(false);
  });

  it("re-pins via scrollToBottom()", () => {
    const el = makeList();
    const ref = createRef<HTMLDivElement>();
    (ref as { current: HTMLDivElement }).current = el;
    const { result } = renderHook(() => useStickToBottom(ref));
    act(() => {
      el.dispatchEvent(Object.assign(new Event("wheel"), { deltaY: -50 }));
    });
    expect(result.current.isPinned).toBe(false);
    act(() => result.current.scrollToBottom());
    expect(result.current.isPinned).toBe(true);
  });

  // tr-9dsz: the "↓" button must be driven by an actual distance THRESHOLD, not
  // the auto-follow sticky flag. A wheel-up while near the bottom detaches
  // auto-follow (isPinned=false) but must NOT pop the button — otherwise it shows
  // on any tiny scroll (incl. a short chat already at the bottom).
  it("keeps the ↓ button hidden on a wheel-up while near the bottom", () => {
    const el = makeList(); // at bottom, distance 0
    const ref = createRef<HTMLDivElement>();
    (ref as { current: HTMLDivElement }).current = el;
    const { result } = renderHook(() => useStickToBottom(ref));
    expect(result.current.showScrollButton).toBe(false);
    act(() => {
      el.dispatchEvent(Object.assign(new Event("wheel"), { deltaY: -50 }));
    });
    expect(result.current.isPinned).toBe(false); // auto-follow detaches
    expect(result.current.showScrollButton).toBe(false); // but button stays hidden
  });

  it("shows the ↓ button only once scrolled past the threshold, hides near the bottom", () => {
    const el = makeList();
    const ref = createRef<HTMLDivElement>();
    (ref as { current: HTMLDivElement }).current = el;
    const { result } = renderHook(() => useStickToBottom(ref));
    expect(result.current.showScrollButton).toBe(false);
    // Scroll well up (distance = 500 - 50 - 100 = 350) → button appears.
    act(() => {
      el.scrollTop = 50;
      el.dispatchEvent(new Event("scroll"));
    });
    expect(result.current.showScrollButton).toBe(true);
    // A small nudge back near the bottom (distance = 20) hides it again.
    act(() => {
      el.scrollTop = 380;
      el.dispatchEvent(new Event("scroll"));
    });
    expect(result.current.showScrollButton).toBe(false);
  });

  // Regression (tr-hsg.9): in a new chat the feed mounts AFTER ChatWindow's first
  // render, so listRef.current is null when the attach effect first runs. The
  // effect must re-run when the feed becomes ready, or listeners never attach and
  // the "↓" button never appears (isPinned stuck true).
  it("attaches listeners when the feed mounts after initial render", () => {
    const ref = createRef<HTMLDivElement>();
    const { result, rerender } = renderHook(
      ({ ready }) => useStickToBottom(ref, ready),
      { initialProps: { ready: false } },
    );
    // Feed mounts now (node appears after the first render).
    const el = makeList();
    (ref as { current: HTMLDivElement }).current = el;
    rerender({ ready: true });
    expect(result.current.isPinned).toBe(true);
    act(() => {
      el.dispatchEvent(Object.assign(new Event("wheel"), { deltaY: -50 }));
    });
    expect(result.current.isPinned).toBe(false);
  });
});
