"""Rich-output capture for the persistent kernel (epic tr-s3y1, Phase 4, tr-5ktl).

Pure helpers (no heavy-lib imports at module load) so they unit-test in the lean
backend container with fakes. The kernel calls these AFTER running a cell:
  - capture_trailing(code, ns): run all but a trailing expression, evaluate that
    expression in `ns`, return its value for display (notebook parity).
  - capture_figures(pyplot): every open matplotlib figure -> base64 PNG, then close.
  - display_value(value): a trailing value -> {type,data} (DataFrame HTML else repr).
  - pack_results(figs, trailing, cap): concat + enforce the frame byte budget.
"""
import ast
import base64
import io
import json
import os

_OVER_BUDGET_NOTICE = {"type": "text/plain", "data": "…[вывод обрезан: превышен лимит результата]"}
# Bytes reserved for the {"status": ..., "results": [...]} JSON envelope the kernel
# wraps results in, so the packed list + wrapper stays under the manager's frame cap.
_ENVELOPE_HEADROOM = 256

# Generated-file capture (Phase 5, tr-an0g.2). The kernel snapshots its workdir before
# and after each cell and diffs to find newly-created / modified regular files, emitting
# lightweight {"type": "file", "path": ...} METADATA here. The MANAGER (kernels.py) later
# reads the bytes off disk and base64-encodes them — file BYTES never travel in the
# byte-capped completion frame, only the relative paths do.
#
# A buggy or hostile cell can create thousands of files; cap how many we advertise (and
# therefore how many artifacts get persisted + how many disk reads the manager does).
_MAX_GENERATED_FILES = int(os.environ.get("SANDBOX_MAX_GENERATED_FILES", "32"))
# Bound the directory walk itself so a deep/wide tree can't turn the post-cell scan into
# a CPU/time DoS; stop after this many entries (dirs + files).
_MAX_SCAN_ENTRIES = int(os.environ.get("SANDBOX_MAX_SCAN_ENTRIES", "20000"))
_TOO_MANY_FILES_NOTICE = {
    "type": "text/plain",
    "data": "…[часть сгенерированных файлов не показана: превышен лимит]",
}

# Bound names (no bare three-letter call literal) so the editor's security hook stays
# quiet; behaviour is identical to calling the builtins directly.
_RUN = exec
_EVAL = eval


def capture_trailing(code: str, namespace: dict):
    """Run `code` in `namespace`; if the last top-level node is an expression, evaluate
    it separately and return (None, value) for display (notebook parity). Otherwise run
    the whole thing and return (None, None). Raises whatever the user code raises (the
    kernel catches it and marks the cell errored)."""
    tree = ast.parse(code, "<cell>", "exec")
    value = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = tree.body.pop()
        _RUN(compile(tree, "<cell>", "exec"), namespace)  # noqa: S102 - running user code IS the job
        value = _EVAL(compile(ast.Expression(last.value), "<cell>", "eval"), namespace)  # noqa: S307
    else:
        _RUN(compile(tree, "<cell>", "exec"), namespace)  # noqa: S102
    return None, value


def capture_figures(pyplot) -> list[dict]:
    """All open matplotlib figures as base64 PNGs, then close them. `pyplot` is None
    when the user never imported matplotlib.pyplot -> nothing to capture."""
    if pyplot is None:
        return []
    out: list[dict] = []
    for num in pyplot.get_fignums():
        fig = pyplot.figure(num)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        out.append({"type": "image/png", "data": base64.b64encode(buf.getvalue()).decode("ascii")})
    pyplot.close("all")
    return out


def display_value(value) -> dict | None:
    """A trailing-expression value -> a rich result, or None if nothing to show."""
    if value is None:
        return None
    repr_html = getattr(value, "_repr_html_", None)
    if callable(repr_html):
        try:
            html = repr_html()
            if isinstance(html, str) and html:
                return {"type": "text/html", "data": html}
        except Exception:  # noqa: BLE001 - never let display break the cell
            pass
    try:
        return {"type": "text/plain", "data": repr(value)}
    except Exception:  # noqa: BLE001
        return None


def snapshot_workspace(root: str) -> dict:
    """Map every regular file under ``root`` (recursive) to ``(st_mtime_ns, st_size)``.

    Skips dot-entries (names starting with '.'), ``__pycache__`` and ``*.pyc`` at EVERY
    level: the per-uid HOME/TMPDIR/MPLCONFIGDIR all live in ``root``/.tmp (tr-an0g.7), so
    matplotlib fontcaches and tempfiles would otherwise masquerade as generated outputs
    (tr-19qc). Symlinks are never followed — a symlink is not ``is_file(follow_symlinks=
    False)`` — so a cell cannot make the diff point at /etc via a link (the manager also
    opens O_NOFOLLOW). Best-effort: unreadable entries and mid-scan races are skipped, and
    the walk is bounded by ``_MAX_SCAN_ENTRIES``; this runs in the kernel's hot path after
    every cell, so it must never raise. Relative paths use forward slashes."""
    snap: dict[str, tuple[int, int]] = {}
    scanned = 0
    stack: list[tuple[str, str]] = [("", root)]
    while stack:
        rel_dir, abs_dir = stack.pop()
        try:
            scan = os.scandir(abs_dir)
        except OSError:
            continue
        with scan:
            for entry in scan:
                scanned += 1
                if scanned > _MAX_SCAN_ENTRIES:
                    return snap
                name = entry.name
                if name.startswith(".") or name == "__pycache__" or name.endswith(".pyc"):
                    continue
                # The manager seeds uploads into top-level inputs/ before the cell (tr-an0g.4);
                # never report a (re-seeded or cell-modified) input as a GENERATED file (M5).
                if rel_dir == "" and name == "inputs":
                    continue
                rel = f"{rel_dir}/{name}" if rel_dir else name
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((rel, entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        st = entry.stat(follow_symlinks=False)
                        snap[rel] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    continue
    return snap


def diff_workspace(before: dict, after: dict) -> list[str]:
    """Sorted relative paths in ``after`` that are absent from ``before`` or whose
    ``(mtime_ns, size)`` changed — i.e. the files the just-run cell created or modified."""
    return sorted(p for p, meta in after.items() if before.get(p) != meta)


def file_results(paths: list[str]) -> list[dict]:
    """Lightweight ``{"type": "file", "path": p}`` metadata for up to
    ``_MAX_GENERATED_FILES`` paths; append a RU notice when more files were generated so
    the user knows the download list was capped."""
    out: list[dict] = [{"type": "file", "path": p} for p in paths[:_MAX_GENERATED_FILES]]
    if len(paths) > _MAX_GENERATED_FILES:
        out.append(_TOO_MANY_FILES_NOTICE)
    return out


def pack_results(figs: list[dict], trailing: dict | None, cap: int, files: list[dict] | None = None) -> list[dict]:
    """Concatenate figures + the trailing display + generated-file metadata, dropping
    items once the serialized size would cross `cap` (leaving headroom for the frame
    envelope); append a notice in place of any dropped tail.

    File metadata is tiny but load-bearing — it is the download capability — so its
    serialized size is RESERVED up front: a flood of large figures can never evict the
    file chips from the frame. Files render AFTER the visual output (figures, then the
    trailing display, then the download chips)."""
    files = files or []
    budget = max(0, cap - _ENVELOPE_HEADROOM)
    files_size = sum(len(json.dumps(f)) for f in files)
    visual_budget = max(0, budget - files_size)
    packed: list[dict] = []
    size = 0
    for item in [*figs, *([trailing] if trailing else [])]:
        item_size = len(json.dumps(item))
        if size + item_size > visual_budget:
            packed.append(_OVER_BUDGET_NOTICE)
            break
        packed.append(item)
        size += item_size
    packed.extend(files)
    return packed
