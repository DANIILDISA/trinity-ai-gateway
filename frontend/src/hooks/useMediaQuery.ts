"use client";

import { useEffect, useState } from "react";

// Reactive CSS media-query match. SSR-safe (returns false on the server, where
// there is no matchMedia) and reads the real value synchronously on the client
// via a lazy initializer so there is no first-frame flash.
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(
    () => typeof window !== "undefined" && window.matchMedia(query).matches,
  );

  useEffect(() => {
    const mql = window.matchMedia(query);
    setMatches(mql.matches);
    const onChange = (e: { matches: boolean }) => setMatches(e.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}
