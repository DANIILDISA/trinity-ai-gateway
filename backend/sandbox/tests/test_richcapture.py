"""Rich-output capture helpers (epic tr-s3y1, Phase 4, tr-5ktl).

These are pure and lib-free, so they run in the lean backend container with fakes
standing in for matplotlib.pyplot / a DataFrame. The real matplotlib/pandas path is
exercised by the baked-image probe in verify_isolation.py.
"""
import base64
import os

import pytest

from sandbox import richcapture


class _FakeFig:
    def __init__(self, payload: bytes):
        self._payload = payload

    def savefig(self, buf, format, **kw):  # noqa: A002 - matplotlib's kw name
        assert format == "png"
        buf.write(self._payload)


class _FakePyplot:
    def __init__(self, figs):
        self._figs = figs
        self.closed = None

    def get_fignums(self):
        return list(range(1, len(self._figs) + 1))

    def figure(self, num):
        return self._figs[num - 1]

    def close(self, which):
        self.closed = which


def test_capture_figures_encodes_each_fignum_as_png():
    pp = _FakePyplot([_FakeFig(b"PNGDATA1"), _FakeFig(b"PNGDATA2")])
    out = richcapture.capture_figures(pp)
    assert [r["type"] for r in out] == ["image/png", "image/png"]
    assert base64.b64decode(out[0]["data"]) == b"PNGDATA1"
    assert pp.closed == "all"  # figures freed after capture


def test_capture_figures_none_when_no_pyplot():
    assert richcapture.capture_figures(None) == []


def test_display_value_dataframe_uses_repr_html():
    class _DF:
        def _repr_html_(self):
            return "<table>x</table>"

    assert richcapture.display_value(_DF()) == {"type": "text/html", "data": "<table>x</table>"}


def test_display_value_plain_repr_for_scalars():
    assert richcapture.display_value(42) == {"type": "text/plain", "data": "42"}


def test_display_value_none_is_dropped():
    assert richcapture.display_value(None) is None


def test_capture_trailing_evaluates_last_expression_only():
    ns = {}
    _, value = richcapture.capture_trailing("a = 5\na + 1", ns)
    assert ns["a"] == 5  # body executed in ns
    assert value == 6


def test_capture_trailing_no_trailing_expr_returns_none_value():
    ns = {}
    _, value = richcapture.capture_trailing("a = 5", ns)
    assert value is None


def test_pack_results_drops_over_budget_and_appends_notice():
    import json

    big = {"type": "image/png", "data": "A" * 100}
    item_bytes = len(json.dumps(big))
    # Budget (cap minus the envelope headroom) fits exactly one item, not two.
    cap = item_bytes + richcapture._ENVELOPE_HEADROOM + 10
    packed = richcapture.pack_results([big, big], None, cap=cap)
    assert packed[0] == big  # first fits
    assert packed[-1]["type"] == "text/plain"  # second dropped, notice appended
    assert "обрез" in packed[-1]["data"]


def test_pack_results_keeps_all_when_under_budget():
    a = {"type": "text/plain", "data": "x"}
    assert richcapture.pack_results([], a, cap=2 * 1024 * 1024) == [a]


# --- Phase 5 (tr-an0g.2): workspace-diff capture of generated files -------------


def test_snapshot_diff_detects_new_and_modified_files(tmp_path):
    root = str(tmp_path)
    (tmp_path / "a.csv").write_text("hello")
    before = richcapture.snapshot_workspace(root)
    (tmp_path / "b.txt").write_text("world")        # brand-new file
    (tmp_path / "a.csv").write_text("hello again")  # size (and mtime) change
    after = richcapture.snapshot_workspace(root)
    assert richcapture.diff_workspace(before, after) == ["a.csv", "b.txt"]


def test_snapshot_no_change_yields_empty_diff(tmp_path):
    (tmp_path / "a.csv").write_text("hello")
    before = richcapture.snapshot_workspace(str(tmp_path))
    after = richcapture.snapshot_workspace(str(tmp_path))
    assert richcapture.diff_workspace(before, after) == []


def test_snapshot_ignores_dot_entries_and_pycache(tmp_path):
    # tr-19qc: per-uid HOME/TMPDIR/MPLCONFIGDIR live in workdir/.tmp, so matplotlib
    # fontcaches and tempfiles (all dot-prefixed) must never surface as generated files.
    before = richcapture.snapshot_workspace(str(tmp_path))
    (tmp_path / ".tmp").mkdir()
    (tmp_path / ".tmp" / ".mpl").mkdir()
    (tmp_path / ".tmp" / ".mpl" / "fontlist.json").write_text("cache")
    (tmp_path / ".hidden").write_text("x")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "m.cpython-312.pyc").write_text("x")
    (tmp_path / "stray.pyc").write_text("x")
    (tmp_path / "real.csv").write_text("data")
    after = richcapture.snapshot_workspace(str(tmp_path))
    assert richcapture.diff_workspace(before, after) == ["real.csv"]


def test_snapshot_excludes_inputs_dir(tmp_path):
    # M5: the manager seeds uploads into top-level inputs/; those must never surface as
    # GENERATED files even when a cell re-seeds or modifies them.
    before = richcapture.snapshot_workspace(str(tmp_path))
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "data.csv").write_text("seeded upload")
    (tmp_path / "out.csv").write_text("generated")
    after = richcapture.snapshot_workspace(str(tmp_path))
    assert richcapture.diff_workspace(before, after) == ["out.csv"]


def test_snapshot_recurses_into_subdirs(tmp_path):
    before = richcapture.snapshot_workspace(str(tmp_path))
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "data.csv").write_text("x")
    after = richcapture.snapshot_workspace(str(tmp_path))
    assert richcapture.diff_workspace(before, after) == ["out/data.csv"]


def test_snapshot_does_not_follow_symlinks(tmp_path):
    # Defence-in-depth (the manager also opens O_NOFOLLOW): a symlink a cell drops into
    # the workdir is not a regular file, so it is never advertised as a generated file.
    (tmp_path / "outside_target").write_text("secret")
    before = richcapture.snapshot_workspace(str(tmp_path))
    try:
        os.symlink(str(tmp_path / "outside_target"), str(tmp_path / "link.txt"))
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlinks unavailable on this platform")
    after = richcapture.snapshot_workspace(str(tmp_path))
    assert "link.txt" not in richcapture.diff_workspace(before, after)


def test_snapshot_missing_root_is_empty(tmp_path):
    assert richcapture.snapshot_workspace(str(tmp_path / "does-not-exist")) == {}


def test_file_results_shape():
    assert richcapture.file_results(["a.csv"]) == [{"type": "file", "path": "a.csv"}]


def test_file_results_empty_for_no_paths():
    assert richcapture.file_results([]) == []


def test_file_results_caps_count_with_notice():
    paths = [f"f{i}.csv" for i in range(richcapture._MAX_GENERATED_FILES + 5)]
    out = richcapture.file_results(paths)
    files = [r for r in out if r["type"] == "file"]
    assert len(files) == richcapture._MAX_GENERATED_FILES
    assert out[-1]["type"] == "text/plain"  # over-limit notice appended
    assert "лимит" in out[-1]["data"]


def test_pack_results_reserves_file_metadata_against_figure_flood():
    import json

    files = [{"type": "file", "path": "out.csv"}]
    files_size = len(json.dumps(files[0]))
    big = {"type": "image/png", "data": "A" * 500}
    # Budget fits the (tiny) file metadata but NOT a big figure on top of it.
    cap = files_size + richcapture._ENVELOPE_HEADROOM + 10
    packed = richcapture.pack_results([big], None, cap=cap, files=files)
    assert {"type": "file", "path": "out.csv"} in packed   # download link survives
    assert big not in packed                               # figure dropped
    assert any(r["type"] == "text/plain" and "обрез" in r["data"] for r in packed)


def test_pack_results_files_render_after_visual_output():
    a = {"type": "text/plain", "data": "x"}
    files = [{"type": "file", "path": "out.csv"}]
    packed = richcapture.pack_results([], a, cap=2 * 1024 * 1024, files=files)
    assert packed == [a, {"type": "file", "path": "out.csv"}]
