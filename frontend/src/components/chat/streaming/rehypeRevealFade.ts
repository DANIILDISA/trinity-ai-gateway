// Minimal hast types (avoid a hard dep on @types/hast).
interface HastText {
  type: "text";
  value: string;
}
interface HastElement {
  type: "element";
  tagName: string;
  properties?: Record<string, unknown>;
  children: HastNode[];
}
interface HastRoot {
  type: "root";
  children: HastNode[];
}
type HastNode = HastText | HastElement | HastRoot | { type: string; children?: HastNode[] };

const SKIP_TAGS = new Set(["pre", "code"]);

function isElement(n: HastNode): n is HastElement {
  return n.type === "element";
}
function isText(n: HastNode): n is HastText {
  return n.type === "text";
}

// KaTeX renders math to <span class="katex">…</span> (and katex-mathml / katex-html
// / katex-display) — a precise nested DOM whose text nodes must NOT be split or
// wrapped, or the formula breaks. It runs BEFORE this plugin, so detect it by
// className (not tagName) and skip the whole subtree.
function isKatex(el: HastElement): boolean {
  const cn = el.properties?.className;
  const classes = Array.isArray(cn)
    ? cn
    : typeof cn === "string"
      ? cn.split(/\s+/)
      : [];
  return classes.some((c) => typeof c === "string" && c.startsWith("katex"));
}

/**
 * Wraps the last `windowSize` words (within the first `revealCount` characters)
 * in animated `.reveal-word` spans. `--reveal-i` is the distance from the tail
 * (0 = newest), driving the staggered fade.
 */
export function rehypeRevealFade(opts: { revealCount: number; windowSize?: number }) {
  const windowSize = opts.windowSize ?? 12;

  return (tree: HastRoot) => {
    // 1. Collect text nodes in document order with their cumulative char offset.
    const texts: { node: HastText; start: number; parent: HastElement | HastRoot }[] = [];
    let offset = 0;
    const walk = (node: HastNode, parent: HastElement | HastRoot, skip: boolean) => {
      if (isText(node)) {
        // Only revealable (non-skipped) text advances the offset so the word
        // index space stays consistent with `allText` below (which excludes
        // skipped subtrees). Skipped text neither counts nor gets wrapped.
        if (!skip) {
          texts.push({ node, start: offset, parent });
          offset += node.value.length;
        }
        return;
      }
      if (isElement(node) || node.type === "root") {
        const el = node as HastElement;
        const nextSkip =
          skip ||
          (isElement(node) && (SKIP_TAGS.has(el.tagName) || isKatex(el)));
        (el.children ?? []).forEach((c) => walk(c, el, nextSkip));
      }
    };
    walk(tree, tree, false);

    // 2. Find word spans (start/end char offsets) across the whole revealed text.
    const fullLen = offset;
    const revealEnd = Math.min(opts.revealCount, fullLen);
    const wordRe = /\S+/g;
    const allText = texts.map((t) => t.node.value).join("");
    const words: { start: number; end: number }[] = [];
    let m: RegExpExecArray | null;
    while ((m = wordRe.exec(allText)) !== null) {
      if (m.index >= revealEnd) break;
      words.push({ start: m.index, end: Math.min(m.index + m[0].length, revealEnd) });
    }
    const windowStart = Math.max(0, words.length - windowSize);
    const animated = words.slice(windowStart); // these get wrapped
    if (animated.length === 0) return;
    const tailStart = animated[0].start; // char offset where the window begins

    // 3. Rewrite each text node: split into [plain prefix][animated word spans].
    for (const entry of texts) {
      const { node, start, parent } = entry;
      const end = start + node.value.length;
      if (end <= tailStart) continue; // entirely settled — leave as-is
      const localPlainEnd = Math.max(0, tailStart - start);
      const newChildren: HastNode[] = [];
      if (localPlainEnd > 0) {
        newChildren.push({ type: "text", value: node.value.slice(0, localPlainEnd) });
      }
      // Re-tokenise the tail portion of this node into words + gaps.
      const tail = node.value.slice(localPlainEnd);
      const re = /(\s+)|(\S+)/g;
      let mm: RegExpExecArray | null;
      while ((mm = re.exec(tail)) !== null) {
        if (mm[1]) {
          newChildren.push({ type: "text", value: mm[1] });
          continue;
        }
        const absStart = start + localPlainEnd + mm.index;
        const idxFromTail = animated.findIndex((w) => w.start === absStart);
        const revealI = idxFromTail >= 0 ? animated.length - 1 - idxFromTail : 0;
        newChildren.push({
          type: "element",
          tagName: "span",
          properties: { className: ["reveal-word"], style: `--reveal-i:${revealI}` },
          children: [{ type: "text", value: mm[2] }],
        } as HastElement);
      }
      const siblings = (parent as HastElement).children;
      const at = siblings.indexOf(node);
      if (at >= 0) siblings.splice(at, 1, ...newChildren);
    }
  };
}
