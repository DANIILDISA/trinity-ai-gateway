// Syntax highlighting primitive shared by the chat code blocks (Markdown.tsx)
// and the artifact canvas panel. Built on Shiki with the JavaScript RegExp
// engine (no WASM — avoids loader issues in the Next 15 / docker setup) and
// fine-grained, lazily-imported grammars so only the languages actually used in
// a session are downloaded. A single highlighter instance is reused per session.

// Canonical language id -> fine-grained grammar loader. The key set IS the
// supported set; anything outside it falls back to a plain (unhighlighted) block.
const LANG_LOADERS: Record<string, () => Promise<unknown>> = {
  typescript: () => import("@shikijs/langs/typescript"),
  javascript: () => import("@shikijs/langs/javascript"),
  tsx: () => import("@shikijs/langs/tsx"),
  jsx: () => import("@shikijs/langs/jsx"),
  python: () => import("@shikijs/langs/python"),
  bash: () => import("@shikijs/langs/bash"),
  json: () => import("@shikijs/langs/json"),
  sql: () => import("@shikijs/langs/sql"),
  css: () => import("@shikijs/langs/css"),
  html: () => import("@shikijs/langs/html"),
  markdown: () => import("@shikijs/langs/markdown"),
  go: () => import("@shikijs/langs/go"),
  rust: () => import("@shikijs/langs/rust"),
  java: () => import("@shikijs/langs/java"),
  c: () => import("@shikijs/langs/c"),
  cpp: () => import("@shikijs/langs/cpp"),
  yaml: () => import("@shikijs/langs/yaml"),
  diff: () => import("@shikijs/langs/diff"),
};

const ALIASES: Record<string, string> = {
  py: "python",
  sh: "bash",
  shell: "bash",
  zsh: "bash",
  yml: "yaml",
  ts: "typescript",
  js: "javascript",
  md: "markdown",
  "c++": "cpp",
  rs: "rust",
  golang: "go",
};

export const FALLBACK_LANG = "text";
const THEME = "one-dark-pro";

/**
 * Normalize a fenced-block language hint to a supported, canonical Shiki
 * language id. Returns FALLBACK_LANG ("text") for missing, empty or
 * unsupported input so callers can render a plain block without highlighting.
 */
export function normalizeLang(language?: string): string {
  if (!language) return FALLBACK_LANG;
  const key = language.toLowerCase().trim();
  if (!key) return FALLBACK_LANG;
  const canonical = ALIASES[key] ?? key;
  return canonical in LANG_LOADERS ? canonical : FALLBACK_LANG;
}

type Highlighter = Awaited<ReturnType<typeof loadHighlighter>>;
let highlighterPromise: Promise<Highlighter> | null = null;
const loadedLangs = new Set<string>();

async function loadHighlighter() {
  const { createHighlighterCore } = await import("shiki/core");
  const { createJavaScriptRegexEngine } = await import("shiki/engine/javascript");
  return createHighlighterCore({
    themes: [import("@shikijs/themes/one-dark-pro")],
    langs: [],
    // `forgiving` keeps the engine from throwing on grammar regexes it can't
    // compile — a block renders (possibly under-highlighted) instead of erroring.
    engine: createJavaScriptRegexEngine({ forgiving: true }),
  });
}

function getHighlighter(): Promise<Highlighter> {
  if (!highlighterPromise) highlighterPromise = loadHighlighter();
  return highlighterPromise;
}

/**
 * Highlight `code` to an HTML string. Returns null when the language is
 * unsupported or highlighting fails — callers then render a plain fallback.
 * Never throws.
 */
export async function highlightToHtml(
  code: string,
  language?: string,
): Promise<string | null> {
  const lang = normalizeLang(language);
  if (lang === FALLBACK_LANG) return null;
  try {
    const hl = await getHighlighter();
    if (!loadedLangs.has(lang)) {
      await hl.loadLanguage(LANG_LOADERS[lang]() as never);
      loadedLangs.add(lang);
    }
    return hl.codeToHtml(code, { lang, theme: THEME });
  } catch {
    return null;
  }
}
