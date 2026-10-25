"""_persistable_results: slim heavy base64 out of persisted tool-step results.

On reload, a persisted binary result (matplotlib image/png, generated workspace file)
re-fetches its bytes from the linked Artifact via artifact_id — so the heavy base64 must
NOT also live in the message row (double storage; audit must-fix tr-an0g #3). An
over-quota file that was never persisted (no artifact_id) keeps its inline base64 so it
still downloads on reload. The LIVE SSE result is untouched (full base64 for inline render).
"""
from app.services.billing_service import _persistable_results


def test_image_png_base64_dropped():
    out = _persistable_results([{"type": "image/png", "data": "AAA", "artifact_id": "x", "filename": "p.png"}])
    assert out == [{"type": "image/png", "artifact_id": "x", "filename": "p.png"}]


def test_persisted_file_base64_dropped_but_metadata_kept():
    out = _persistable_results(
        [{"type": "file", "data": "BBB", "artifact_id": "y", "filename": "data.csv",
          "media_type": "text/csv", "size": 8}]
    )
    assert out == [
        {"type": "file", "artifact_id": "y", "filename": "data.csv", "media_type": "text/csv", "size": 8}
    ]


def test_small_unpersisted_file_keeps_inline_base64():
    # No artifact_id (over quota) but SMALL → keep base64 inline so reload still downloads it.
    r = {"type": "file", "data": "CCC", "filename": "x.csv", "media_type": "text/csv", "size": 3}
    assert _persistable_results([r]) == [r]


def test_large_unpersisted_binary_slimmed_with_marker():
    # Audit B4: a large over-quota file (no artifact_id) must NOT keep its base64 in the
    # message row forever — drop it and mark it unavailable on reload.
    big = "A" * (300 * 1024)
    out = _persistable_results(
        [{"type": "file", "data": big, "filename": "huge.csv", "media_type": "text/csv", "size": 999}]
    )
    assert "data" not in out[0]
    assert out[0].get("unavailable") is True
    assert out[0]["filename"] == "huge.csv"  # metadata kept so the chip can still render


def test_small_unpersisted_image_png_kept_inline():
    # Audit L1: previously image/png was slimmed unconditionally (no artifact_id guard) →
    # an over-quota PNG 404'd on reload. A small un-persisted PNG now keeps its data.
    r = {"type": "image/png", "data": "A" * 1000}
    assert _persistable_results([r]) == [r]


def test_text_results_kept_inline():
    rows = [{"type": "text/plain", "data": "hi"}, {"type": "text/html", "data": "<i>x</i>"}]
    assert _persistable_results(rows) == rows


def test_none_and_empty():
    assert _persistable_results(None) is None
    assert _persistable_results([]) is None
