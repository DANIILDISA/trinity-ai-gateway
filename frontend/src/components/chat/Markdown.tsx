"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";

import { CodeHighlight } from "./CodeHighlight";
import { rehypeRevealFade } from "./streaming/rehypeRevealFade";

function CodeIcon({ done }: { done: boolean }) {
  return done ? (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M5 13l4 4L19 7"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  ) : (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="9" y="9" width="11" height="11" rx="2" stroke="currentColor" strokeWidth="1.5" />
      <path d="M5 15V5a2 2 0 0 1 2-2h10" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

// Claude-style code block: a quiet inset panel with a language label + copy
// control at the top (no divider line) and a SINGLE horizontal scroll only when
// needed — no always-on / doubled scrollbars. `not-prose` isolates it from the
// surrounding typography styles. Exported so code inside the thinking block
// (reasoning fenced blocks) renders identically to the main answer (tr-cy0.3.19).
export function CodeBlock({ language, code }: { language?: string; code: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard unavailable (insecure context) — silently ignore.
    }
  };
  return (
    <div className="not-prose my-4 overflow-hidden rounded-lg bg-[#161615]">
      <div className="flex items-center justify-between px-4 pt-2.5">
        <span className="font-mono text-[11px] tracking-wide text-neutral-500">
          {language ?? "code"}
        </span>
        <button
          type="button"
          onClick={onCopy}
          className="flex items-center gap-1 text-[11px] text-neutral-500 transition-colors hover:text-neutral-300"
        >
          <CodeIcon done={copied} />
          {copied ? "Скопировано" : "Копировать"}
        </button>
      </div>
      <CodeHighlight language={language} code={code} />
    </div>
  );
}

// KaTeX strictness (tr-g1j): the RU market routinely writes Cyrillic in math
// mode (e.g. $S_{между}$). KaTeX's default strict='warn' logs one warning per
// Cyrillic char — ≈197 on a single answer — flooding the console for a render
// that is in fact readable. Ignore ONLY that diagnostic; every other LaTeX issue
// still warns. throwOnError:false keeps one malformed formula from blanking the
// whole message.
const katexOptions = {
  throwOnError: false,
  strict: (code: string): "ignore" | "warn" =>
    code === "unicodeTextInMathMode" ? "ignore" : "warn",
};

const components: Components = {
  // Unwrap the default <pre> so CodeBlock provides its own container (no nested
  // <pre>). The block markup comes from the `code` override below.
  pre({ children }) {
    return <>{children}</>;
  },
  // Claude-style links: citations/sources open in a new tab so a click never
  // throws the user out of the chat. Visual styling (neutral, underlined — NOT
  // blue) comes from the prose-a:* utilities on the wrapper.
  a({ href, children }) {
    return (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    );
  },
  code({ className, children }) {
    const text = String(children ?? "");
    const match = /language-(\w+)/.exec(className ?? "");
    const isBlock = !!match || text.includes("\n");
    if (isBlock) {
      return <CodeBlock language={match?.[1]} code={text.replace(/\n$/, "")} />;
    }
    return (
      <code className="rounded-[5px] bg-white/[0.08] px-1.5 py-0.5 font-mono text-[0.85em] text-neutral-200">
        {children}
      </code>
    );
  },
};

// remark-math treats `$…$` as inline math, so a sentence like "$5 today and $10"
// is parsed as math — the digits are italicised and the dollar signs eaten. For a
// ruble/USD app that silently corrupts money amounts (tr-ymda). A `$` immediately
// before a digit is currency, not a math delimiter, so escape it (`\$`) — it then
// renders literally. Single-`$` LaTeX that does NOT start with a digit ($x^2$,
// $S_{между}$) is untouched and still renders. Code spans/fences are skipped so we
// never inject a visible backslash into code.
function escapeCurrencyDollars(md: string): string {
  return md
    .split(/(```[\s\S]*?```|`[^`]*`)/g)
    .map((segment, i) =>
      i % 2 === 1 ? segment : segment.replace(/\$(?=\d)/g, "\\$"),
    )
    .join("");
}

// Renders assistant markdown in the Claude aesthetic: serif body, no surrounding
// box, generous line-height, restrained heading sizes, subtle rules, and the
// custom code blocks above. The smooth word reveal + the loading mark are the
// streaming affordances — no blinking text cursor.
export function Markdown({
  content,
  revealCount,
}: {
  content: string;
  revealCount?: number;
}) {
  const rehypePlugins =
    revealCount === undefined
      ? [[rehypeKatex, katexOptions]]
      : [[rehypeKatex, katexOptions], [rehypeRevealFade, { revealCount }]];
  return (
    <div className="prose prose-invert max-w-none break-words font-serif text-[15px] leading-[1.75] text-neutral-200 [overflow-wrap:anywhere] prose-headings:font-semibold prose-headings:tracking-tight prose-headings:text-neutral-100 prose-h1:text-[1.4em] prose-h2:text-[1.25em] prose-h3:text-[1.1em] prose-p:my-3 prose-a:font-medium prose-a:text-neutral-100 prose-a:underline prose-a:decoration-neutral-600 prose-a:underline-offset-2 prose-a:transition-colors hover:prose-a:decoration-neutral-400 hover:prose-a:text-white prose-strong:font-semibold prose-strong:text-neutral-100 prose-ul:my-3 prose-ol:my-3 prose-li:my-1 prose-hr:my-6 prose-hr:border-neutral-700/40 prose-blockquote:border-l-2 prose-blockquote:border-neutral-700 prose-blockquote:not-italic prose-blockquote:font-normal prose-blockquote:text-neutral-400 prose-table:my-4 prose-table:w-full prose-table:text-sm prose-thead:border-b prose-thead:border-neutral-700 prose-th:px-3 prose-th:py-1.5 prose-th:text-left prose-th:font-semibold prose-th:text-neutral-100 prose-td:border-t prose-td:border-neutral-800 prose-td:px-3 prose-td:py-1.5 prose-td:text-neutral-300">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={rehypePlugins as never}
        components={components}
      >
        {escapeCurrencyDollars(content)}
      </ReactMarkdown>
    </div>
  );
}
