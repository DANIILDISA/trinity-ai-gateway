"""Deterministic one-line titles for reasoning segments (tr-cy0.3.5).

Product decision (spike tr-cy0.3.1 + user): segment titles are a FREE heuristic.
Trinity is a pure proxy, so an extra model call to summarize a segment would be
real money forwarded to OpenRouter — and per the user's decision we DO NOT make
that call. The title is therefore derived deterministically from the text:

  (a) a Russian provider summary, normalized; otherwise
  (b) the segment's lead line (the markdown-heading / bold-lead it was split on,
      or its first sentence), cleaned to one short line.

There is no LLM path and no db/balance dependency, so the user balance is never
charged (billing invariant). English reasoning yields its English lead — honest
given the no-LLM constraint; Russian dialogs (Trinity's market) think in Russian,
so the lead is Russian in the common case.
"""
import re

# One short line, in the reference style; the reference headers run ~70 chars.
_SUMMARY_MAX_LEN = 80

# `**bold**` → bold (unwrap before stripping leading markers, which would eat the
# asterisks first).
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
# Leading markdown structure: heading hashes, list bullets, block-quote markers.
_LEAD_MARKERS_RE = re.compile(r"^[\s>#*\-+]+")
# Stray inline emphasis / code marks. Underscores and tildes are left alone so
# identifiers like `Python_exam_prep` survive intact.
_INLINE_MD_RE = re.compile(r"[`*]")
_WS_RE = re.compile(r"\s+")
# A sentence boundary: terminator followed by whitespace.
_SENTENCE_END_RE = re.compile(r"[.!?]\s")
# Punctuation/dashes we trim from the ends of a title.
_TRIM_CHARS = " \t\r\n.,:;—–-…"
# Contentless opener fillers the model uses before the substantive thought
# (tr-c14): interjections ("Хорошо,", "Окей,", "Итак,") and their English peers.
# Matched only when followed by a separator, so real words ("Около", "Также",
# "Some", "Wells") are never clipped. The outer `(?:…)+` peels stacked fillers
# ("Окей, итак,") in one pass. Task-restating labels ("Пользователь просит:") are
# handled separately by the trailing-colon rule, not listed here.
_PREAMBLE_PREFIX_RE = re.compile(
    r"^(?:(?:хорошо|ладно|итак|хм+|окей|ок|ну|что ж|значит|"
    r"okay|ok|alright|well|so|now|hmm+|right|"
    r"let me think|let me see|let'?s think|let'?s see)"
    r"[\s,.:;—–-]+)+",
    re.IGNORECASE,
)


def _has_cyrillic(text: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in text)


def _pick_summary_line(text: str) -> str:
    """First SUBSTANTIVE line for a title, skipping contentless lead-ins (tr-c14).

    Skips task-restating label lines (a cleaned line ending in ``:`` that defers
    its content to the next line, e.g. ``Пользователь просит:``) and lines that
    are nothing but filler interjections. A leading filler on an otherwise
    substantive line is stripped. Falls back to the first non-empty line when no
    line is more substantive, so a lone label still yields a (trimmed) title
    rather than empty.
    """
    first_nonempty = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not first_nonempty:
            first_nonempty = line
        # Clean enough to judge content (mirror normalize's early steps) WITHOUT
        # trimming the trailing colon — that colon is the label signal.
        probe = _BOLD_RE.sub(r"\1", line)
        probe = _LEAD_MARKERS_RE.sub("", probe)
        probe = _INLINE_MD_RE.sub("", probe)
        probe = _WS_RE.sub(" ", probe).strip()
        if probe.endswith(":"):
            continue  # task-restating label; the content is on a later line
        stripped = _PREAMBLE_PREFIX_RE.sub("", probe).strip()
        if not stripped:
            continue  # the whole line was filler
        # Original line when nothing was peeled (preserves prior behaviour); the
        # filler-stripped form otherwise. normalize_summary re-cleans either way.
        return line if stripped == probe else stripped
    return first_nonempty


def _truncate_words(line: str) -> str:
    if len(line) <= _SUMMARY_MAX_LEN:
        return line
    budget = _SUMMARY_MAX_LEN - 1  # leave room for the ellipsis
    words = line.split(" ")
    out: list[str] = []
    total = 0
    for w in words:
        add = (1 if out else 0) + len(w)
        if total + add > budget:
            break
        out.append(w)
        total += add
    head = " ".join(out) if out else line[:budget]
    return head + "…"


def normalize_summary(raw: str) -> str:
    """Clean an arbitrary lead/summary into one short title line: unwrap bold,
    drop heading/list markers and inline emphasis, collapse whitespace, keep the
    first sentence, strip trailing punctuation, truncate by words (≤ the limit,
    `…` when cut), and capitalize the first letter. Skips contentless preamble
    lead-ins when picking the line (tr-c14)."""
    line = _pick_summary_line(raw)
    if not line:
        return ""
    line = _BOLD_RE.sub(r"\1", line)
    line = _LEAD_MARKERS_RE.sub("", line)
    line = _INLINE_MD_RE.sub("", line)
    line = _WS_RE.sub(" ", line).strip()
    m = _SENTENCE_END_RE.search(line)
    if m:
        line = line[: m.start() + 1]
    line = line.strip(_TRIM_CHARS)
    if not line:
        return ""
    line = _truncate_words(line)
    return line[0].upper() + line[1:]


def summarize_segment(
    text: str,
    *,
    provider_summary: str | None = None,
    dialog_lang: str = "ru",
) -> str:
    """Return a deterministic one-line title for a reasoning segment.

    A Russian provider summary is normalized and used; otherwise the title is the
    normalized lead line of `text`. `dialog_lang` is accepted for forward
    compatibility but does not trigger translation (no LLM by design). Never
    raises on odd input — returns "" when nothing usable is found.
    """
    if provider_summary and _has_cyrillic(provider_summary):
        normalized = normalize_summary(provider_summary)
        if normalized:
            return normalized
    return normalize_summary(text)
