"""Manager-side workdir file reader (epic tr-s3y1, Phase 5.3, tr-an0g.3).

The kernel's workspace diff emits {type:'file', path} metadata; the MANAGER then reads
those bytes off the per-conversation workdir (the backend has no FS access, and the 2MB
completion frame is byte-capped). The path is ATTACKER-CONTROLLED — untrusted code can
forge the completion frame via SANDBOX_RESULT_FD — so every read is hardened: realpath
must stay under the workdir, O_NOFOLLOW + O_NONBLOCK + S_ISREG refuse symlinks/FIFOs/
devices/dirs, and per-file / per-cell / count caps bound the bytes pulled into the
(un-frame-capped) HTTP response. These tests drive the pure reader directly.
"""
import base64
import os

import pytest

from sandbox import kernels


def _file_results(out):
    return [r for r in out if isinstance(r, dict) and r.get("type") == "file"]


def test_reads_regular_file_into_base64(tmp_path):
    (tmp_path / "out.csv").write_text("a,b\n1,2\n")
    out = kernels.attach_file_bytes(str(tmp_path), [{"type": "file", "path": "out.csv"}])
    assert len(out) == 1
    r = out[0]
    assert r["type"] == "file"
    assert r["filename"] == "out.csv"
    assert base64.b64decode(r["data"]) == b"a,b\n1,2\n"
    assert r["media_type"] == "text/csv"
    assert r["size"] == len("a,b\n1,2\n")


def test_reads_file_in_subdir(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "data.bin").write_bytes(b"\x00\x01\x02")
    out = kernels.attach_file_bytes(str(tmp_path), [{"type": "file", "path": "out/data.bin"}])
    assert base64.b64decode(out[0]["data"]) == b"\x00\x01\x02"
    assert out[0]["filename"] == "data.bin"  # L2: download name is the basename, not the subdir path
    assert out[0]["media_type"] == "application/octet-stream"


def test_same_basename_in_different_subdirs_deduped(tmp_path):
    """L2 (tr-dtc8): two generated files with the same basename in different subdirs must get
    distinct download names so the browser doesn't overwrite one with the other."""
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "data.csv").write_text(f"from {d}\n")
    out = kernels.attach_file_bytes(
        str(tmp_path),
        [{"type": "file", "path": "a/data.csv"}, {"type": "file", "path": "b/data.csv"}],
    )
    names = [r["filename"] for r in out if r.get("type") == "file"]
    assert names == ["data.csv", "data(1).csv"]
    assert len(set(names)) == 2


def test_non_file_results_pass_through_unchanged(tmp_path):
    inputs = [
        {"type": "image/png", "data": "AAA"},
        {"type": "text/plain", "data": "hi"},
    ]
    out = kernels.attach_file_bytes(str(tmp_path), list(inputs))
    assert out == inputs


def test_path_traversal_rejected(tmp_path):
    # A real file OUTSIDE the workdir must never be read via ../ .
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET")
    workdir = tmp_path / "wd"
    workdir.mkdir()
    out = kernels.attach_file_bytes(str(workdir), [{"type": "file", "path": "../outside.txt"}])
    assert _file_results(out) == []
    assert out and out[-1]["type"] == "text/plain"  # dropped-notice appended


def test_absolute_path_rejected(tmp_path):
    workdir = tmp_path / "wd"
    workdir.mkdir()
    out = kernels.attach_file_bytes(str(workdir), [{"type": "file", "path": "/etc/hostname"}])
    assert _file_results(out) == []


def test_symlink_final_component_rejected(tmp_path):
    # O_NOFOLLOW: a symlink whose target sits OUTSIDE the workdir must not be followed.
    secret = tmp_path / "secret.txt"
    secret.write_text("PII")
    workdir = tmp_path / "wd"
    workdir.mkdir()
    try:
        os.symlink(str(secret), str(workdir / "link.txt"))
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlinks unavailable on this platform")
    out = kernels.attach_file_bytes(str(workdir), [{"type": "file", "path": "link.txt"}])
    assert _file_results(out) == []


def test_fifo_rejected_without_hanging(tmp_path):
    # A FIFO opened O_NONBLOCK returns instantly; S_ISREG then refuses it (the audit's
    # FIFO-open-hang fix). If this regresses the open() blocks and the test times out.
    try:
        os.mkfifo(str(tmp_path / "pipe"))
    except (OSError, AttributeError, NotImplementedError):
        pytest.skip("mkfifo unavailable on this platform")
    out = kernels.attach_file_bytes(str(tmp_path), [{"type": "file", "path": "pipe"}])
    assert _file_results(out) == []


def test_directory_path_rejected(tmp_path):
    (tmp_path / "adir").mkdir()
    out = kernels.attach_file_bytes(str(tmp_path), [{"type": "file", "path": "adir"}])
    assert _file_results(out) == []


def test_missing_file_dropped(tmp_path):
    out = kernels.attach_file_bytes(str(tmp_path), [{"type": "file", "path": "ghost.csv"}])
    assert _file_results(out) == []


def test_per_file_cap_drops_oversized_file(tmp_path):
    (tmp_path / "big.bin").write_bytes(b"x" * 100)
    out = kernels.attach_file_bytes(
        str(tmp_path), [{"type": "file", "path": "big.bin"}], per_file_cap=10
    )
    assert _file_results(out) == []  # dropped whole, never truncated
    # A file exactly at the cap is kept.
    (tmp_path / "ok.bin").write_bytes(b"x" * 10)
    out2 = kernels.attach_file_bytes(
        str(tmp_path), [{"type": "file", "path": "ok.bin"}], per_file_cap=10
    )
    assert len(_file_results(out2)) == 1


def test_total_cell_cap_drops_overflow(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 8)
    (tmp_path / "b.bin").write_bytes(b"y" * 8)
    out = kernels.attach_file_bytes(
        str(tmp_path),
        [{"type": "file", "path": "a.bin"}, {"type": "file", "path": "b.bin"}],
        total_cap=10,
    )
    files = _file_results(out)
    assert len(files) == 1  # first fits (8 <= 10); second would exceed → dropped
    assert out[-1]["type"] == "text/plain"


def test_null_byte_in_path_rejected_without_raising(tmp_path):
    # Audit M2: a forged path with a NUL byte makes os.path.realpath raise ValueError
    # (not OSError) — it must be caught and dropped, never propagate to a kernel reset.
    out = kernels.attach_file_bytes(str(tmp_path), [{"type": "file", "path": "a\x00b.csv"}])
    assert _file_results(out) == []


def test_filename_canonicalised_and_control_chars_stripped(tmp_path):
    # Audit M1: the stored filename must be the CANONICAL workdir-relative path with ASCII
    # control chars (CR/LF) stripped — no attacker string reaches Artifact.filename / a
    # Content-Disposition header. The file is read by its real (control-char) name, but the
    # RESULT filename is sanitised.
    weird = "re\r\nport.csv"
    (tmp_path / weird).write_bytes(b"data")
    out = kernels.attach_file_bytes(str(tmp_path), [{"type": "file", "path": weird}])
    files = _file_results(out)
    assert len(files) == 1
    assert "\r" not in files[0]["filename"] and "\n" not in files[0]["filename"]
    assert files[0]["filename"] == "report.csv"


def test_filename_canonical_collapses_dotdot(tmp_path):
    # Audit M1 (dedup): 'sub/../b.csv' canonicalises to 'b.csv' so identical bytes don't
    # persist twice under two names.
    (tmp_path / "b.csv").write_bytes(b"data")
    out = kernels.attach_file_bytes(str(tmp_path), [{"type": "file", "path": "sub/../b.csv"}])
    files = _file_results(out)
    assert len(files) == 1
    assert files[0]["filename"] == "b.csv"


def test_executable_media_types_forced_to_octet_stream(tmp_path):
    # Audit B1: a generated .html/.svg/.js must NOT be served with its executable content
    # type (it would run when opened from the OS file manager) — force octet-stream.
    for name in ("evil.html", "chart.svg", "payload.js"):
        (tmp_path / name).write_text("<x/>")
    out = kernels.attach_file_bytes(
        str(tmp_path), [{"type": "file", "path": n} for n in ("evil.html", "chart.svg", "payload.js")]
    )
    files = _file_results(out)
    assert len(files) == 3
    assert all(f["media_type"] == "application/octet-stream" for f in files)


def test_safe_media_types_preserved(tmp_path):
    (tmp_path / "a.csv").write_text("x")
    (tmp_path / "b.json").write_text("{}")
    (tmp_path / "c.png").write_bytes(b"x")
    out = kernels.attach_file_bytes(
        str(tmp_path), [{"type": "file", "path": n} for n in ("a.csv", "b.json", "c.png")]
    )
    mt = {f["filename"]: f["media_type"] for f in _file_results(out)}
    assert mt == {"a.csv": "text/csv", "b.json": "application/json", "c.png": "image/png"}


def test_bidi_control_char_stripped_from_filename(tmp_path):
    # Audit M4: a BiDi override (U+202E) survives the ord>=0x20 filter and spoofs the
    # displayed extension in the chip label — strip Unicode control/format chars.
    weird = "data‮.csv"
    (tmp_path / weird).write_text("x")
    out = kernels.attach_file_bytes(str(tmp_path), [{"type": "file", "path": weird}])
    files = _file_results(out)
    assert len(files) == 1
    assert "‮" not in files[0]["filename"]


def test_unknown_result_type_dropped(tmp_path):
    # Audit S1: only an allowlisted set of result types crosses the manager boundary; a
    # forged type is dropped (defence-in-depth), allowlisted ones pass through.
    out = kernels.attach_file_bytes(
        str(tmp_path),
        [{"type": "application/x-evil", "data": "x"}, {"type": "text/plain", "data": "ok"}],
    )
    types = [r.get("type") for r in out]
    assert "application/x-evil" not in types
    assert "text/plain" in types


def test_max_files_count_capped(tmp_path):
    paths = []
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x")
        paths.append({"type": "file", "path": f"f{i}.txt"})
    out = kernels.attach_file_bytes(str(tmp_path), paths, max_files=3)
    assert len(_file_results(out)) == 3
    assert out[-1]["type"] == "text/plain"  # over-count notice
