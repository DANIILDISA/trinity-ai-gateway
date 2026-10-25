"use client";

import { useEffect, useRef, useState } from "react";

import { Markdown } from "../Markdown";
import { useStreamReveal } from "./useStreamReveal";

// Keep the reveal plugin mounted this long after the buffer catches up, so the
// trailing words finish their fade naturally. Removing the plugin earlier cuts
// those animations short → the snap-to-opaque is the "micro-flicker" at the end.
// Comfortably exceeds the fade duration (380ms) + max stagger (11 × 26ms ≈ 286ms)
// in globals.css (.reveal-word), so removal lands after every word is opaque.
const FADE_TAIL_MS = 700;

/**
 * Streaming-aware Markdown: reveals `content` word-by-word with a trailing fade
 * while it streams, while the buffer is still catching up after completion, and
 * for a short tail afterwards so the last words finish fading before the plugin
 * is removed (no end-of-stream flicker). Historical (never-streamed) content
 * renders whole and plain immediately.
 */
export function RevealMarkdown({
  content,
  streaming,
}: {
  content: string;
  streaming: boolean;
}) {
  const { revealed, revealedCount } = useStreamReveal(content, streaming);

  // Only messages that actually streamed get the reveal treatment; a reloaded /
  // historical message (streaming=false from mount) renders plain.
  const everStreamed = useRef(false);
  if (streaming) everStreamed.current = true;

  const draining = revealed.length < content.length;
  const caughtUp =
    everStreamed.current && !streaming && !draining && content.length > 0;

  // After catch-up, hold the plugin for FADE_TAIL_MS so the last fades complete,
  // then drop it cleanly (by which point every word is already fully opaque).
  const [tailDone, setTailDone] = useState(false);
  useEffect(() => {
    if (!caughtUp) {
      setTailDone(false);
      return;
    }
    const t = setTimeout(() => setTailDone(true), FADE_TAIL_MS);
    return () => clearTimeout(t);
  }, [caughtUp]);

  const revealing =
    streaming || draining || (everStreamed.current && !tailDone && content.length > 0);

  return (
    <Markdown
      content={revealed}
      revealCount={revealing ? revealedCount : undefined}
    />
  );
}
