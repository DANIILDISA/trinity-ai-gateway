import { useCallback, useEffect, useRef, useState } from "react";

// Auto-follow detaches once the user scrolls this far from the bottom.
const SCROLL_STICKY_PX = 100;
// The "↓" button appears only once the user is THIS far from the bottom — a real
// threshold (well past the sticky one), so it shows only on a long feed scrolled
// noticeably up, never on any tiny shift or a short chat already at the bottom
// (tr-9dsz).
const SCROLL_BUTTON_PX = 200;

/**
 * Keeps the feed pinned to the bottom during streaming, but yields immediately to
 * an upward scroll. `isPinned` (state) drives auto-follow; `showScrollButton`
 * (a separate distance THRESHOLD, decoupled from the wheel-unpin) drives the "↓"
 * button; `scrollToBottom` re-attaches. `notifyGrowth` should be called when
 * content height grows.
 */
export function useStickToBottom(
  listRef: React.RefObject<HTMLDivElement | null>,
  // Flips true once the feed (listRef's node) is actually mounted. The feed often
  // mounts AFTER the first render (e.g. a new chat starts on the empty state), so
  // the attach effect must re-run when this becomes true — otherwise listRef is
  // null on the one-time run and listeners never attach (tr-hsg.9).
  feedReady?: boolean,
) {
  const sticky = useRef(true);
  const programmatic = useRef(false);
  const [isPinned, setIsPinned] = useState(true);
  const [showScrollButton, setShowScrollButton] = useState(false);

  const setSticky = useCallback((v: boolean) => {
    if (sticky.current !== v) {
      sticky.current = v;
      setIsPinned(v);
    }
  }, []);

  // The "↓" button is purely a function of how far we are from the bottom — not
  // of the auto-follow flag — so a wheel-up near the bottom (which detaches
  // auto-follow) never pops it (tr-9dsz).
  const refreshButton = useCallback(() => {
    const list = listRef.current;
    if (!list) return;
    const distance = list.scrollHeight - list.scrollTop - list.clientHeight;
    setShowScrollButton(distance > SCROLL_BUTTON_PX);
  }, [listRef]);

  useEffect(() => {
    const list = listRef.current;
    if (!list) return;

    const onScroll = () => {
      if (programmatic.current) return;
      const distance = list.scrollHeight - list.scrollTop - list.clientHeight;
      setSticky(distance < SCROLL_STICKY_PX);
      setShowScrollButton(distance > SCROLL_BUTTON_PX);
    };
    const onWheel = (e: WheelEvent) => {
      if (e.deltaY < 0) setSticky(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (["PageUp", "ArrowUp", "Home"].includes(e.key)) setSticky(false);
    };

    list.addEventListener("scroll", onScroll, { passive: true });
    list.addEventListener("wheel", onWheel, { passive: true });
    list.addEventListener("keydown", onKey);
    // Seed the button state for the mounted (possibly already scrolled) feed.
    refreshButton();
    return () => {
      list.removeEventListener("scroll", onScroll);
      list.removeEventListener("wheel", onWheel);
      list.removeEventListener("keydown", onKey);
    };
  }, [listRef, setSticky, refreshButton, feedReady]);

  // Pin instantly during growth (caller invokes on content change).
  const notifyGrowth = useCallback(() => {
    const list = listRef.current;
    if (!list) return;
    if (sticky.current) {
      programmatic.current = true;
      list.scrollTop = list.scrollHeight;
      requestAnimationFrame(() => {
        programmatic.current = false;
      });
    }
    // Content grew while detached (scrolled up): the bottom moved further away,
    // so refresh the button without waiting for a scroll event.
    refreshButton();
  }, [listRef, refreshButton]);

  const scrollToBottom = useCallback(() => {
    const list = listRef.current;
    if (!list) return;
    setSticky(true);
    setShowScrollButton(false);
    const reduce =
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (typeof list.scrollTo === "function") {
      list.scrollTo({ top: list.scrollHeight, behavior: reduce ? "auto" : "smooth" });
    }
  }, [listRef, setSticky]);

  return { isPinned, showScrollButton, notifyGrowth, scrollToBottom, setSticky };
}
