import { act, renderHook } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useScrollRestoration } from "./useScrollRestoration";

// Drive rAF manually so the multi-frame "re-apply until content settles" loop is
// deterministic. flushFrames(n) runs n queued frames.
let queue: FrameRequestCallback[] = [];
beforeEach(() => {
  queue = [];
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    queue.push(cb);
    return queue.length;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
});
afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

function flushFrames(n: number) {
  for (let i = 0; i < n; i++) {
    const cbs = queue;
    queue = [];
    act(() => cbs.forEach((cb) => cb(performance.now())));
  }
}

// A list whose scrollTop CLAMPS to [0, scrollHeight - clientHeight], like the real
// DOM, and whose scrollHeight can grow (content settling after first paint).
function makeGrowableList(scrollHeight: number) {
  const el = document.createElement("div");
  let height = scrollHeight;
  let top = 0;
  Object.defineProperty(el, "clientHeight", { value: 100, configurable: true });
  Object.defineProperty(el, "scrollHeight", {
    get: () => height,
    configurable: true,
  });
  Object.defineProperty(el, "scrollTop", {
    get: () => top,
    set: (v: number) => {
      top = Math.max(0, Math.min(v, height - 100));
    },
    configurable: true,
  });
  document.body.appendChild(el);
  return {
    el,
    grow: (h: number) => {
      height = h;
    },
  };
}

function refTo(el: HTMLDivElement) {
  const ref = createRef<HTMLDivElement>();
  (ref as { current: HTMLDivElement }).current = el;
  return ref;
}

describe("useScrollRestoration", () => {
  it("restores a saved position on mount", () => {
    sessionStorage.setItem("trinity:scroll:c1", "220");
    const { el } = makeGrowableList(500); // max = 400, target 220 fits
    renderHook(() => useScrollRestoration(refTo(el), "c1", true));
    expect(el.scrollTop).toBe(220);
  });

  it("persists position on scroll once restore has settled", () => {
    const { el } = makeGrowableList(500); // no saved → target = bottom
    renderHook(() => useScrollRestoration(refTo(el), "c2", true));
    flushFrames(5); // let the settle loop finish (height stable)
    act(() => {
      el.scrollTop = 333;
      el.dispatchEvent(new Event("scroll"));
    });
    expect(sessionStorage.getItem("trinity:scroll:c2")).toBe("333");
  });

  // Regression (tr-hsg.10): content lays out TALLER after the pre-paint restore, so
  // the first scrollTop write clamps short. The loop must re-apply the target as
  // scrollHeight grows, landing on the saved value (not the clamped one).
  it("re-applies the saved target as content grows taller (no clamp)", () => {
    sessionStorage.setItem("trinity:scroll:c3", "3284");
    const { el, grow } = makeGrowableList(1261); // max = 1161 → 3284 clamps to 1161
    renderHook(() => useScrollRestoration(refTo(el), "c3", true));
    expect(el.scrollTop).toBe(1161); // first apply clamps
    grow(7298); // content finished laying out (max = 7198)
    flushFrames(3);
    expect(el.scrollTop).toBe(3284); // re-applied to the true target
  });

  // Regression (tr-hsg.10): the persist listener must NOT overwrite the saved
  // target with the clamped value while restoration is still in progress.
  it("does not clobber the saved target while restoring", () => {
    sessionStorage.setItem("trinity:scroll:c4", "3284");
    const { el } = makeGrowableList(1261); // 3284 clamps to 1161 initially
    renderHook(() => useScrollRestoration(refTo(el), "c4", true));
    // A scroll event fires from the clamped write while still restoring.
    act(() => el.dispatchEvent(new Event("scroll")));
    expect(sessionStorage.getItem("trinity:scroll:c4")).toBe("3284");
  });
});
