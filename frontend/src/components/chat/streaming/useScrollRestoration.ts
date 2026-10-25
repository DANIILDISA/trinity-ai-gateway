import { useEffect, useLayoutEffect, useRef } from "react";

const KEY = (id: string) => `trinity:scroll:${id}`;
const MAX_SETTLE_FRAMES = 60; // ~1s safety cap
const STABLE_FRAMES = 3; // height unchanged this many frames → settled

/**
 * Persists and restores per-conversation scroll position. `ready` should be true
 * once the feed (listRef's node) is actually mounted. Returns a `restored` ref so
 * the caller can suppress its first auto-scroll.
 *
 * Restoration is instant + pre-paint, then RE-APPLIED across a few frames: message
 * content lays out taller after the first paint (markdown reflow, images), so a
 * one-shot write clamps short. We keep re-applying the saved target until the
 * content height settles or the target is reached — and suppress the persist write
 * meanwhile so the clamped value never overwrites the saved one (tr-hsg.10). A user
 * scroll/wheel/key aborts the re-apply so their intent always wins.
 */
export function useScrollRestoration(
  listRef: React.RefObject<HTMLDivElement | null>,
  conversationId: string,
  ready: boolean,
) {
  const restored = useRef(false);
  const restoring = useRef(false);

  useLayoutEffect(() => {
    restored.current = false;
    if (!ready) return;
    const list = listRef.current;
    if (!list || !conversationId) return;

    const saved = sessionStorage.getItem(KEY(conversationId));
    // No saved value → restore to the bottom (latest message).
    const target = saved != null ? Number(saved) : Number.POSITIVE_INFINITY;
    restoring.current = true;

    let raf = 0;
    let frames = 0;
    let lastHeight = -1;
    let stable = 0;

    const finish = () => {
      raf = 0;
      restoring.current = false;
      restored.current = true;
    };

    const apply = () => {
      const el = listRef.current;
      if (!el) {
        finish();
        return;
      }
      const max = el.scrollHeight - el.clientHeight;
      el.scrollTop = Number.isFinite(target) ? Math.min(target, max) : max;

      if (el.scrollHeight === lastHeight) stable += 1;
      else stable = 0;
      lastHeight = el.scrollHeight;
      frames += 1;

      const reachedTarget = Number.isFinite(target) && el.scrollTop >= target - 1;
      if (reachedTarget || stable >= STABLE_FRAMES || frames >= MAX_SETTLE_FRAMES) {
        finish();
        return;
      }
      raf = requestAnimationFrame(apply);
    };

    apply(); // immediate, pre-paint

    // User intent wins: any manual scroll input ends the re-apply early.
    const abort = () => {
      if (raf) cancelAnimationFrame(raf);
      finish();
    };
    list.addEventListener("wheel", abort, { passive: true, once: true });
    list.addEventListener("touchstart", abort, { passive: true, once: true });
    list.addEventListener("keydown", abort, { once: true });

    return () => {
      if (raf) cancelAnimationFrame(raf);
      list.removeEventListener("wheel", abort);
      list.removeEventListener("touchstart", abort);
      list.removeEventListener("keydown", abort);
      restoring.current = false;
    };
  }, [listRef, conversationId, ready]);

  // Persist on scroll. Suppressed while restoring so the clamped first write can't
  // overwrite the saved target. `ready` is a dep so the listener (re)attaches when
  // the feed mounts late.
  useEffect(() => {
    const list = listRef.current;
    if (!list || !conversationId) return;
    const onScroll = () => {
      if (restoring.current) return;
      sessionStorage.setItem(KEY(conversationId), String(list.scrollTop));
    };
    list.addEventListener("scroll", onScroll, { passive: true });
    return () => list.removeEventListener("scroll", onScroll);
  }, [listRef, conversationId, ready]);

  return { restored };
}
