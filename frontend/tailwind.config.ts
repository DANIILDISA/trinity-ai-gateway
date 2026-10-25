import type { Config } from "tailwindcss";
import typography from "@tailwindcss/typography";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        // Assistant answer bodies (Claude-style). Loaded via next/font in the
        // root layout; falls back to system serifs.
        serif: ["var(--font-serif)", "Georgia", "Cambria", "serif"],
      },
      colors: {
        trinity: {
          canvas: "#0a0a0a",
          panel: "#171717",
          elevated: "#262626",
          hover: "#404040",
          border: "#262626",
          borderStrong: "#404040",
          accent: "#059669",
          accentHover: "#10b981",
          accentSubtle: "#34d399",
        },
      },
      keyframes: {
        // Left-to-right gradient sweep for skeleton/loading placeholders.
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
        // Lightbox backdrop fade + image pop-in.
        fadeIn: {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        scaleIn: {
          "0%": { opacity: "0", transform: "scale(0.95)" },
          "100%": { opacity: "1", transform: "scale(1)" },
        },
        slideIn: {
          from: { opacity: "0", transform: "translateX(-8px)" },
          to: { opacity: "1", transform: "translateX(0)" },
        },
        slideUp: {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        shimmer: "shimmer 1.5s infinite",
        fadeIn: "fadeIn 200ms ease-out",
        scaleIn: "scaleIn 200ms ease-out",
        slideIn: "slideIn 150ms ease-out",
        slideUp: "slideUp 200ms ease-out",
      },
    },
  },
  plugins: [typography],
};

export default config;
