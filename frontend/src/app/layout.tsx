import type { Metadata } from "next";
import { Source_Serif_4 } from "next/font/google";
import "./globals.css";

import { ChromeFrame } from "@/components/layout/ChromeFrame";

// Elegant serif for assistant answer bodies (Claude-style). Cyrillic + Latin,
// exposed as a CSS variable and consumed via Tailwind's `font-serif`. The rest
// of the UI keeps the sans default.
const serif = Source_Serif_4({
  subsets: ["latin", "cyrillic"],
  display: "swap",
  variable: "--font-serif",
});

export const metadata: Metadata = {
  title: "Trinity",
  description: "AI-модели за рубли",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="ru" className={serif.variable}>
      <body className="flex min-h-screen flex-col bg-[var(--trinity-canvas)] text-neutral-100 antialiased">
        <div className="flex-1">{children}</div>
        <ChromeFrame />
      </body>
    </html>
  );
}
