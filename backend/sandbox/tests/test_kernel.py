"""Persistent kernel: framed protocol, state persistence, hardening (tr-gbpc Ф3.2/Ф3.3).

The kernel is a long-lived child (sandbox.kernel) that reads length-prefixed code
frames from stdin, runs each in a PERSISTENT namespace, and writes a small
completion frame to a dedicated result fd. User stdout/stderr stay on fd1/fd2.
These tests drive it directly (no manager), mirroring test_runner.py/test_seccomp.py.
"""
import fcntl
import json
import os
import select
import struct
import subprocess
import sys
import time


def _spawn(env_extra=None, cwd=None):
    rfd, wfd = os.pipe()  # result channel: kernel writes wfd, test reads rfd
    proc = subprocess.Popen(
        [sys.executable, "-I", "-m", "sandbox.kernel"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,  # the kernel's workdir == its spawn cwd (manager passes the per-conv dir)
        pass_fds=(wfd,),
        env={**os.environ, "SANDBOX_RESULT_FD": str(wfd), **(env_extra or {})},
    )
    os.close(wfd)  # only the child keeps the write end
    for f in (proc.stdout, proc.stderr):  # non-blocking user-output pipes
        fl = fcntl.fcntl(f.fileno(), fcntl.F_GETFL)
        fcntl.fcntl(f.fileno(), fcntl.F_SETFL, fl | os.O_NONBLOCK)
    return proc, rfd


def _read_n(fd, n, timeout=20):
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while len(buf) < n:
        r, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
        if not r:
            raise TimeoutError("result frame timeout")
        chunk = os.read(fd, n - len(buf))
        if not chunk:
            raise EOFError("result fd closed")
        buf += chunk
    return bytes(buf)


def _drain(f):
    chunks = []
    while True:
        try:
            b = os.read(f.fileno(), 65536)
        except BlockingIOError:
            break
        if not b:
            break
        chunks.append(b)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _converse(proc, rfd, code):
    # tr-ea8q wire protocol: code frame is [len][8-byte nonce][utf8 code]; the kernel echoes
    # the nonce in its [len][8-byte nonce][utf8 json] completion frame.
    nonce = os.urandom(8)
    cb = code.encode("utf-8")
    proc.stdin.write(struct.pack("<I", 8 + len(cb)) + nonce + cb)
    proc.stdin.flush()
    hdr = _read_n(rfd, 4)
    (length,) = struct.unpack("<I", hdr)
    body = _read_n(rfd, length)
    assert body[:8] == nonce  # kernel must echo the cell's nonce
    frame = json.loads(body[8:].decode("utf-8"))
    time.sleep(0.05)  # kernel flushed fd1/fd2 before the frame; let bytes settle
    return _drain(proc.stdout), _drain(proc.stderr), frame


def test_state_persists_across_frames():
    proc, rfd = _spawn()
    try:
        _converse(proc, rfd, "x = 41")
        out, _err, frame = _converse(proc, rfd, "print(x + 1)")
        assert frame["status"] == "ok"
        assert out.strip() == "42"
    finally:
        proc.kill()
        proc.wait()


def test_seccomp_applied_in_kernel():
    proc, rfd = _spawn()
    try:
        _out, err, frame = _converse(proc, rfd, "import socket; socket.socket()")
        assert frame["status"] == "error"
        assert "PermissionError" in err
    finally:
        proc.kill()
        proc.wait()


def test_exception_keeps_kernel_alive():
    proc, rfd = _spawn()
    try:
        _converse(proc, rfd, "y = 7")
        _out, err, frame = _converse(proc, rfd, "raise ValueError('boom')")
        assert frame["status"] == "error"
        assert "ValueError: boom" in err
        out, _err3, frame3 = _converse(proc, rfd, "print(y)")
        assert frame3["status"] == "ok"
        assert out.strip() == "7"
    finally:
        proc.kill()
        proc.wait()


def test_system_exit_does_not_kill_kernel():
    proc, rfd = _spawn()
    try:
        _converse(proc, rfd, "import sys; sys.exit(3)")
        out, _err, frame = _converse(proc, rfd, "print('alive')")
        assert frame["status"] == "ok"
        assert out.strip() == "alive"
    finally:
        proc.kill()
        proc.wait()


def test_generated_files_surface_in_results(tmp_path):
    """Phase 5 (tr-an0g.2): a cell that writes a file into the workdir gets that file's
    relative path back as {type:'file', path} metadata in the completion frame."""
    proc, rfd = _spawn(cwd=str(tmp_path))
    try:
        _out, _err, frame = _converse(proc, rfd, "open('out.csv', 'w').write('a,b\\n1,2\\n')")
        assert frame["status"] == "ok"
        files = [r for r in frame["results"] if r.get("type") == "file"]
        assert [f["path"] for f in files] == ["out.csv"]
    finally:
        proc.kill()
        proc.wait()


def test_workdir_diff_excludes_dot_entries(tmp_path):
    """tr-19qc: writes into a dot-dir (mimicking the per-uid TMPDIR/MPLCONFIGDIR) must NOT
    surface as generated files — only the real output does."""
    proc, rfd = _spawn(cwd=str(tmp_path))
    try:
        _out, _err, frame = _converse(
            proc, rfd,
            "import os\n"
            "os.mkdir('.cache')\n"
            "open('.cache/font.json', 'w').write('x')\n"
            "open('result.txt', 'w').write('y')\n",
        )
        assert frame["status"] == "ok"
        files = [r["path"] for r in frame["results"] if r.get("type") == "file"]
        assert files == ["result.txt"]
    finally:
        proc.kill()
        proc.wait()


def test_file_written_before_error_is_still_surfaced(tmp_path):
    """Audit M6: a cell that writes a file then raises still surfaces the file (it exists on
    disk and is the user's), while the cell is marked errored."""
    proc, rfd = _spawn(cwd=str(tmp_path))
    try:
        _out, _err, frame = _converse(
            proc, rfd,
            "open('partial.csv', 'w').write('a,b\\n1,2\\n')\nraise ValueError('boom')\n",
        )
        assert frame["status"] == "error"
        files = [r["path"] for r in frame["results"] if r.get("type") == "file"]
        assert files == ["partial.csv"]
    finally:
        proc.kill()
        proc.wait()


def test_unchanged_workdir_yields_no_file_results(tmp_path):
    """A cell that writes nothing produces no file metadata (and a pre-existing file that
    the cell does not touch is not re-reported)."""
    (tmp_path / "seed.csv").write_text("pre-existing")
    proc, rfd = _spawn(cwd=str(tmp_path))
    try:
        _out, _err, frame = _converse(proc, rfd, "z = 1 + 1")
        assert frame["status"] == "ok"
        assert [r for r in frame["results"] if r.get("type") == "file"] == []
    finally:
        proc.kill()
        proc.wait()


def test_cumulative_cpu_backstop_kills_kernel():
    """tr-3bcf: a persistent kernel can't reset consumed CPU, so a cumulative
    RLIMIT_CPU backstop must terminate a CPU-burning conversation (SIGXCPU) instead
    of letting it spin forever."""
    import pytest

    proc, rfd = _spawn(env_extra={"SANDBOX_KERNEL_CPU_SECONDS": "1"})
    try:
        with pytest.raises((EOFError, TimeoutError)):
            _converse(proc, rfd, "while True:\n    pass")
        deadline = time.monotonic() + 5
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert proc.poll() is not None  # killed by the cumulative CPU backstop
    finally:
        proc.kill()
        proc.wait()
