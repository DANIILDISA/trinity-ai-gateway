"""Manager-side upload seeding into the kernel workdir (epic tr-s3y1, Phase 5.4, tr-an0g.4).

The backend forwards the conversation's included chat_files in the /execute payload; the
MANAGER writes them into workdir/inputs/ (cleaned + re-seeded every call) BEFORE the code
frame, so the cell can read inputs/<name>. The filename is semi-trusted (it came from a
user upload), so _safe_input_name reduces it to a NUL-free basename (no path traversal),
and the write uses O_CREAT|O_EXCL|O_NOFOLLOW into a freshly-created dir. Per-file / total
byte caps bound the (64m shared) tmpfs. These tests drive the seeder directly (uid=None,
the test-container default — the chown branch needs CAP_CHOWN and runs only in the sandbox
container).
"""
import base64
import os

import pytest

from sandbox import kernels


def _b64(s: bytes) -> str:
    return base64.b64encode(s).decode("ascii")


def test_safe_input_name_reduces_to_basename():
    assert kernels._safe_input_name("data.csv") == "data.csv"
    assert kernels._safe_input_name("../../etc/passwd") == "passwd"
    assert kernels._safe_input_name("/abs/path/x.bin") == "x.bin"
    assert kernels._safe_input_name("sub/dir/y.txt") == "y.txt"


def test_safe_input_name_rejects_unsafe():
    assert kernels._safe_input_name("") is None
    assert kernels._safe_input_name(".") is None
    assert kernels._safe_input_name("..") is None
    assert kernels._safe_input_name("bad\x00name") is None
    assert kernels._safe_input_name(None) is None


def test_safe_input_name_rejects_overlong_utf8():
    # Audit M1: a Cyrillic name over 255 BYTES (not chars) would hit ENAMETOOLONG and be
    # silently dropped — reject it up front rather than truncate a multibyte char.
    assert kernels._safe_input_name("д" * 200 + ".csv") is None  # 200×2 + 4 = 404 bytes
    assert kernels._safe_input_name("a" * 200 + ".csv") == "a" * 200 + ".csv"  # 204 bytes OK


def test_execute_request_bounds_file_count_and_size():
    # Audit M3: the HTTP boundary rejects an oversized files[] before decode.
    from sandbox.server import ExecuteRequest, InputFile

    with pytest.raises(Exception):
        ExecuteRequest(code="x", files=[{"filename": f"f{i}", "base64": "AA=="} for i in range(65)])
    with pytest.raises(Exception):
        InputFile(filename="x" * 256, base64="AA==")


def test_seed_writes_files_into_inputs(tmp_path):
    seeded = kernels.seed_inputs(
        str(tmp_path),
        [{"filename": "data.csv", "base64": _b64(b"a,b\n1,2\n"), "media_type": "text/csv"}],
        uid=None,
    )
    assert seeded == ["data.csv"]
    assert (tmp_path / "inputs" / "data.csv").read_bytes() == b"a,b\n1,2\n"


def test_seed_traversal_name_lands_inside_inputs(tmp_path):
    kernels.seed_inputs(
        str(tmp_path),
        [{"filename": "../../escape.txt", "base64": _b64(b"x")}],
        uid=None,
    )
    # Reduced to a basename and written INSIDE inputs/, never the parent.
    assert (tmp_path / "inputs" / "escape.txt").read_bytes() == b"x"
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path.parent / "escape.txt").exists()


def test_seed_cleans_and_reseeds_each_call(tmp_path):
    kernels.seed_inputs(str(tmp_path), [{"filename": "old.csv", "base64": _b64(b"1")}], uid=None)
    kernels.seed_inputs(str(tmp_path), [{"filename": "new.csv", "base64": _b64(b"2")}], uid=None)
    assert not (tmp_path / "inputs" / "old.csv").exists()  # stale input removed
    assert (tmp_path / "inputs" / "new.csv").read_bytes() == b"2"


def test_seed_empty_files_clears_stale_inputs(tmp_path):
    # A previous call seeded a file; a later call with no files must clear it and leave
    # no inputs/ dir behind (e.g. the user excluded the upload from context).
    kernels.seed_inputs(str(tmp_path), [{"filename": "x.csv", "base64": _b64(b"1")}], uid=None)
    seeded = kernels.seed_inputs(str(tmp_path), [], uid=None)
    assert seeded == []
    assert not (tmp_path / "inputs").exists()


def test_seed_skips_oversized_file(tmp_path):
    seeded = kernels.seed_inputs(
        str(tmp_path),
        [{"filename": "big.bin", "base64": _b64(b"x" * 100)}],
        uid=None,
        per_file_cap=10,
    )
    assert seeded == []
    assert not (tmp_path / "inputs" / "big.bin").exists()


def test_seed_total_cap_skips_overflow(tmp_path):
    seeded = kernels.seed_inputs(
        str(tmp_path),
        [
            {"filename": "a.bin", "base64": _b64(b"x" * 8)},
            {"filename": "b.bin", "base64": _b64(b"y" * 8)},
        ],
        uid=None,
        total_cap=10,
    )
    assert seeded == ["a.bin"]  # first fits (8<=10); second would exceed → skipped


def test_seed_skips_invalid_base64(tmp_path):
    seeded = kernels.seed_inputs(
        str(tmp_path), [{"filename": "x.csv", "base64": "!!!not base64!!!"}], uid=None
    )
    assert seeded == []


# --- B3 (tr-f63e): read-only inputs/ (M4) + ENOSPC re-seed signal (M1) ---

import errno  # noqa: E402


def test_inputs_dir_and_files_are_read_only(tmp_path):
    """M4: inputs/ is seeded root-owned read-only (0555 dir / 0444 files) so untrusted kernel
    code can READ its uploads but cannot modify/delete them — a probe-match is then equivalent
    to a fresh seed."""
    kernels.seed_inputs(str(tmp_path), [{"filename": "a.csv", "base64": _b64(b"a"), "media_type": "text/csv"}])
    inp = tmp_path / "inputs"
    assert (inp.stat().st_mode & 0o777) == 0o555
    assert ((inp / "a.csv").stat().st_mode & 0o777) == 0o444
    assert (inp / "a.csv").read_bytes() == b"a"


def test_seed_inputs_raises_on_enospc(tmp_path, monkeypatch):
    """M1: a transient ENOSPC mid-seed must propagate so the kernel re-seeds next cell instead
    of silently caching a partial inputs/."""
    def boom(fd, data):
        raise OSError(errno.ENOSPC, "no space")
    monkeypatch.setattr(os, "write", boom)
    with pytest.raises(OSError):
        kernels.seed_inputs(str(tmp_path), [{"filename": "a.csv", "base64": _b64(b"a"), "media_type": "text/csv"}])


def test_deterministic_skip_does_not_raise(tmp_path):
    """A stable per-file skip (here: over the 0-byte cap) is silent — those files are skipped
    every seed regardless, so the result IS cacheable; only transient ENOSPC forces a re-seed."""
    seeded = kernels.seed_inputs(
        str(tmp_path),
        [{"filename": "ok.csv", "base64": _b64(b"a"), "media_type": "text/csv"}],
        per_file_cap=0,
    )
    assert seeded == []


# --- SB-3 (tr-f63e audit pass#2): inputs/ integrity check on probe-match ---


def test_inputs_intact_true_for_root_owned_seeded_dir(tmp_path):
    kernels.seed_inputs(str(tmp_path), [{"filename": "a.csv", "base64": _b64(b"a"), "media_type": "text/csv"}])
    assert kernels._inputs_intact(str(tmp_path)) is True


def test_inputs_intact_false_when_missing(tmp_path):
    assert kernels._inputs_intact(str(tmp_path)) is False


def test_inputs_intact_false_when_uid_owned(tmp_path):
    """A kernel that renamed the root-owned inputs/ away and substituted its OWN dir leaves a
    non-root-owned inputs/ — the integrity check must reject it so the probe forces a re-seed."""
    kernels.seed_inputs(str(tmp_path), [{"filename": "a.csv", "base64": _b64(b"a"), "media_type": "text/csv"}])
    os.chown(str(tmp_path / "inputs"), 12345, 12345)  # simulate the dropped-uid substitution
    assert kernels._inputs_intact(str(tmp_path)) is False


def test_seed_inputs_recovers_from_mkdir_eexist(tmp_path, monkeypatch):
    """L3 (tr-dtc8): a leftover bg thread can re-create inputs/ between the initial rmtree and
    the mkdir → EEXIST. seed_inputs must clear + retry once, not silently skip seeding."""
    (tmp_path / "inputs").mkdir()  # stands in for the thread's re-created dir
    calls = {"n": 0}
    real_rmtree = kernels.shutil.rmtree

    def flaky_rmtree(path, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return  # first (routine) rmtree no-ops → the pre-existing dir survives → mkdir EEXIST
        return real_rmtree(path, **kw)

    monkeypatch.setattr(kernels.shutil, "rmtree", flaky_rmtree)
    seeded = kernels.seed_inputs(
        str(tmp_path), [{"filename": "a.csv", "base64": _b64(b"a"), "media_type": "text/csv"}]
    )
    assert seeded == ["a.csv"]
    assert (tmp_path / "inputs" / "a.csv").read_bytes() == b"a"


def test_seed_inputs_does_not_follow_symlink_at_inputs(tmp_path, monkeypatch):
    """L3 defence-in-depth (tr-dtc8): if a hostile symlink sits at inputs/ when the mkdir hits
    EEXIST, seed_inputs unlinks it (not rmtree, which would leave it) so the retry mkdir makes
    a REAL dir and files are not written through the link to its target."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "inputs").symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(kernels.shutil, "rmtree", lambda path, **kw: None)  # initial clear no-ops
    seeded = kernels.seed_inputs(
        str(tmp_path), [{"filename": "a.csv", "base64": _b64(b"a"), "media_type": "text/csv"}]
    )
    assert seeded == ["a.csv"]
    inp = tmp_path / "inputs"
    assert inp.is_dir() and not inp.is_symlink()
    assert (inp / "a.csv").read_bytes() == b"a"
    assert not (target / "a.csv").exists()  # never wrote through the symlink
