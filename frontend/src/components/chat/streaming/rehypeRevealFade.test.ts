import { describe, expect, it } from "vitest";
import rehypeParse from "rehype-parse";
import rehypeStringify from "rehype-stringify";
import { unified } from "unified";

import { rehypeRevealFade } from "./rehypeRevealFade";

function run(html: string, revealCount: number): string {
  return unified()
    .use(rehypeParse, { fragment: true })
    .use(rehypeRevealFade, { revealCount, windowSize: 3 })
    .use(rehypeStringify)
    .processSync(html)
    .toString();
}

describe("rehypeRevealFade", () => {
  it("wraps only the trailing window of words", () => {
    // 6 words, reveal all; window=3 → last 3 words wrapped, first 3 plain.
    const out = run("<p>one two three four five six</p>", 26);
    expect(out).toContain('class="reveal-word"');
    expect(out.match(/reveal-word/g)?.length).toBe(3);
    expect(out).toContain("one two three"); // earlier words untouched
  });

  it("leaves code blocks untouched", () => {
    const out = run("<pre><code>const x = 1</code></pre>", 11);
    expect(out).not.toContain("reveal-word");
  });

  // Regression: KaTeX renders math to <span class="katex">…</span>; its internal
  // text nodes must never be split/wrapped (breaks the formula) and must not throw
  // off the word offset of surrounding prose.
  it("does not wrap inside KaTeX subtrees", () => {
    const html =
      '<p>before <span class="katex"><span class="katex-mathml">x squared</span></span> after the formula</p>';
    const out = run(html, html.length);
    // No reveal-word span lands inside the katex subtree.
    const katexChunk = out.slice(
      out.indexOf('class="katex"'),
      out.indexOf("</span></span>") + "</span></span>".length,
    );
    expect(katexChunk).not.toContain("reveal-word");
    // Surrounding prose still gets the trailing fade.
    expect(out).toContain('class="reveal-word"');
  });
});
