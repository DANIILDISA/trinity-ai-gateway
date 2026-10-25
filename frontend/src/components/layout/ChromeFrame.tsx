"use client";

import { useEffect } from "react";
import { usePathname } from "next/navigation";

import { CookieConsent } from "./CookieConsent";
import { Footer } from "./Footer";

// The chat route is a full-bleed canvas: it owns its own scroll container, so
// the global Footer + CookieConsent (which sit below {children}) would push
// the document past the viewport and produce a second outer scrollbar. We hide
// both on /chat and lock body overflow there. Every other route renders the
// chrome as before.
export function ChromeFrame() {
  const pathname = usePathname() ?? "/";
  const isChat = pathname.startsWith("/chat");

  useEffect(() => {
    if (!isChat) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [isChat]);

  if (isChat) return null;
  return (
    <>
      <Footer />
      <CookieConsent />
    </>
  );
}

export default ChromeFrame;
