"use client";

import { useEffect, useState } from "react";

import { highlightToHtml } from "@/lib/highlight";

// Renders a code block body with Shiki syntax highlighting. Until the (lazy,
// async) highlighter resolves — and on the server, and for unsupported
// languages — it shows an identical monospace fallback so there is no layout
// shift when the colored version swaps in. The outer container, language label
// and copy control are owned by the caller (CodeBlock).
export function CodeHighlight({
  code,
  language,
}: {
  code: string;
  language?: string;
}) {
  const [html, setHtml] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setHtml(null);
    highlightToHtml(code, language).then((out) => {
      if (active) setHtml(out);
    });
    return () => {
      active = false;
    };
  }, [code, language]);

  if (html) {
    // Shiki emits its own <pre class="shiki"><code>…</code></pre> with an inline
    // theme background. Strip that background/padding/margin (stylesheet
    // !important beats the inline non-important style) and own the box here so
    // it matches the fallback exactly.
    return (
      <div
        className="overflow-x-auto px-4 pb-3.5 pt-2 font-mono text-[12.5px] leading-relaxed [&_code]:!bg-transparent [&_pre]:!m-0 [&_pre]:!bg-transparent [&_pre]:!p-0"
        dangerouslySetInnerHTML={{ __html: html }}
      />
    );
  }

  return (
    <pre className="overflow-x-auto px-4 pb-3.5 pt-2">
      <code className="font-mono text-[12.5px] leading-relaxed text-neutral-200">
        {code}
      </code>
    </pre>
  );
}
