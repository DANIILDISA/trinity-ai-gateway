import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useStreamReveal } from "./useStreamReveal";

// Drive rAF manually: each flushFrames() call runs N queued frames.
let queue: FrameRequestCallback[] = [];
beforeEach(() => {
  queue = [];
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    queue.push(cb);
    return queue.length;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
  vi.stubGlobal(
    "matchMedia",
    (q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }),
  );
});
afterEach(() => vi.unstubAllGlobals());

function flushFrames(n: number) {
  for (let i = 0; i < n; i++) {
    const cbs = queue;
    queue = [];
    act(() => cbs.forEach((cb) => cb(performance.now())));
  }
}

describe("useStreamReveal", () => {
  it("reveals progressively and never exceeds the target", () => {
    const { result, rerender } = renderHook(
      ({ t, s }) => useStreamReveal(t, s),
      { initialProps: { t: "", s: true } },
    );
    rerender({ t: "Hello brave new world", s: true });
    // A few frames: word-alignment rounds early partial chars down to the start of
    // "Hello" (0); after the first whole word has buffered, revealedCount > 0.
    flushFrames(3);
    expect(result.current.revealedCount).toBeGreaterThan(0);
    expect(result.current.revealedCount).toBeLessThan(21);
    flushFrames(50);
    expect(result.current.revealed).toBe("Hello brave new world");
  });

  it("never splits a word (slices on whitespace)", () => {
    const { result, rerender } = renderHook(
      ({ t, s }) => useStreamReveal(t, s),
      { initialProps: { t: "supercalifragilistic expialidocious", s: true } },
    );
    rerender({ t: "supercalifragilistic expialidocious", s: true });
    flushFrames(1);
    const r = result.current.revealed;
    expect(r === "" || /\S$/.test(r) === false || r === "supercalifragilistic expialidocious").toBe(true);
  });

  it("eases the final tail in (drains, does not snap) after completion", () => {
    const { result, rerender } = renderHook(
      ({ t, s }) => useStreamReveal(t, s),
      { initialProps: { t: "a long enough answer here", s: true } },
    );
    rerender({ t: "a long enough answer here", s: true });
    flushFrames(1); // some revealed, still behind
    // Stream completes — must NOT snap the remaining tail in at once.
    rerender({ t: "a long enough answer here", s: false });
    expect(result.current.revealed.length).toBeLessThan(
      "a long enough answer here".length,
    );
    // …it drains smoothly to the full text over the next frames.
    flushFrames(30);
    expect(result.current.revealed).toBe("a long enough answer here");
  });

  it("shows historical (never-streamed) content in full immediately", () => {
    const { result } = renderHook(() =>
      useStreamReveal("already complete answer", false),
    );
    expect(result.current.revealed).toBe("already complete answer");
  });

  // The loop stops once caught up (no idle rAF spin) and re-arms when more content
  // arrives — reveal must continue after the pause.
  it("resumes revealing after catching up when new content arrives", () => {
    const { result, rerender } = renderHook(
      ({ t, s }) => useStreamReveal(t, s),
      { initialProps: { t: "first chunk", s: true } },
    );
    rerender({ t: "first chunk", s: true });
    flushFrames(20);
    expect(result.current.revealed).toBe("first chunk"); // caught up, loop idle
    // More tokens arrive after the pause.
    rerender({ t: "first chunk and a second chunk arrives", s: true });
    flushFrames(20);
    expect(result.current.revealed).toBe("first chunk and a second chunk arrives");
  });
});
