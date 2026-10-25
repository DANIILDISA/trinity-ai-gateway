"use client";

import { createContext, useContext } from "react";
import type { ReactNode } from "react";

// Surface tone for shared UI primitives (Button, Input). The authenticated
// (main) shell renders on a dark canvas and sets tone="dark"; auth/admin layouts
// provide no ToneProvider, so they keep the default light styling untouched.
export type Tone = "light" | "dark";

const ToneContext = createContext<Tone>("light");

export function ToneProvider({
  tone,
  children,
}: {
  tone: Tone;
  children: ReactNode;
}) {
  return <ToneContext.Provider value={tone}>{children}</ToneContext.Provider>;
}

export function useTone(): Tone {
  return useContext(ToneContext);
}
