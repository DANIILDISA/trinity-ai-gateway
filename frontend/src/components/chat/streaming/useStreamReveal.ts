import { useCallback, useEffect, useReducer, useRef } from "react";

const MIN_CPF = 2; // min chars revealed per frame
const MAX_CPF = 64; // cap chars/frame so the reveal never dumps a chunk (soft)
const DRAIN_DIVISOR = 10; // backlog / this = chars this frame (smooth ease-out)

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

// Round a raw char count down to a word boundary so a half-word never renders.
// Never rounds below 0; at/over length returns length.
function toWordBoundary(s: string, n: number): number {
  if (n >= s.length) return s.length;
  let i = n;
  while (i > 0 && !/\s/.test(s[i - 1])) i--;
  return i;
}

/**
 * Decouples display from network: `revealed` advances toward `target` on a rAF
 * loop. Returns the revealed prefix (word-aligned) and its char count.
 */
export function useStreamReveal(
  target: string,
  isStreaming: boolean,
): { revealed: string; revealedCount: number } {
  const revealedRef = useRef(0);
  const targetRef = useRef(target);
  targetRef.current = target;
  // True once this mount has ever been streaming. Distinguishes "just finished
  // streaming" (drain the tail smoothly) from "never streamed" (historical /
  // reloaded message → show whole, no animation).
  const everStreamedRef = useRef(false);
  if (isStreaming) everStreamedRef.current = true;
  const rafRef = useRef<number | null>(null);
  const [, force] = useReducer((n: number) => n + 1, 0);

  // New/shorter target (new message or edit) → don't reveal past it.
  if (target.length < revealedRef.current) revealedRef.current = target.length;

  // One reveal step per frame. Stops (rafRef = null) once caught up so we don't
  // spin an idle rAF loop for the whole stream; the re-arm effect below restarts
  // it when new content arrives.
  const tick = useCallback(() => {
    const len = targetRef.current.length;
    const backlog = len - revealedRef.current;
    if (backlog <= 0) {
      rafRef.current = null;
      return;
    }
    // Always advance by a bounded, eased step — never snap the whole tail in at
    // once (that instant jump is the "jerk at the end" on fast models that
    // deliver big chunks). A large backlog drains over several frames, smoothly.
    const step = Math.min(
      MAX_CPF,
      Math.max(MIN_CPF, Math.ceil(backlog / DRAIN_DIVISOR)),
    );
    revealedRef.current = Math.min(len, revealedRef.current + step);
    force();
    rafRef.current = requestAnimationFrame(tick);
  }, []);

  const cancel = useCallback(() => {
    if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
  }, []);

  useEffect(() => {
    // Reduced motion, or settled content that never streamed in this mount
    // (historical / reloaded message): show it whole, no animation.
    if (prefersReducedMotion() || (!isStreaming && !everStreamedRef.current)) {
      revealedRef.current = targetRef.current.length;
      cancel();
      force();
      return cancel;
    }
    // Streaming OR just finished: drain the remaining backlog smoothly to the end
    // (NO snap on message_done) so the final tokens ease in instead of popping in
    // at once. The loop self-stops once caught up.
    if (rafRef.current == null && targetRef.current.length > revealedRef.current) {
      rafRef.current = requestAnimationFrame(tick);
    }
    return cancel;
  }, [isStreaming, tick, cancel]);

  // Re-arm the loop when new content arrives (during streaming) or there is still
  // a tail to drain (just after completion) and it had caught up and stopped.
  // Runs every render — a cheap guarded check, no extra scheduling while alive.
  useEffect(() => {
    if (
      everStreamedRef.current &&
      rafRef.current == null &&
      !prefersReducedMotion() &&
      targetRef.current.length > revealedRef.current
    ) {
      rafRef.current = requestAnimationFrame(tick);
    }
  });

  const raw = Math.min(revealedRef.current, target.length);
  const revealedCount = toWordBoundary(target, raw);
  return { revealed: target.slice(0, revealedCount), revealedCount };
}
