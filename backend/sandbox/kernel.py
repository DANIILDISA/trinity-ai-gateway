"""Persistent per-conversation kernel (epic tr-s3y1, Phase 3, tr-gbpc).

A long-lived child spawned per conversation. It reads length-prefixed code frames
from stdin, runs each in a PERSISTENT namespace (variables/imports survive across
frames, like a Code-Interpreter session), and writes a small completion frame to a
dedicated result fd. User stdout/stderr stay on the real fd1/fd2 — the manager
drains and caps them, exactly as in the stateless path.

NO sockets are ever used: the seccomp network/signal block (sandbox.seccomp) is
applied here unchanged, right before the loop, so the load-bearing socket()/connect()
denylist still holds for all executed code.

Wire protocol (binary, little-endian):
  stdin              : [4-byte len][utf8 code]   — one execution; EOF -> exit 0
  result fd (env SANDBOX_RESULT_FD): [4-byte len][utf8 json]   — completion frame
      {"status": "ok"|"error", "results": []}    (Phase 4 fills `results`)
"""
import json
import os
import struct
import sys
import threading
import traceback

from sandbox import limits, richcapture, seccomp

# Hard cap on the completion-frame length — rich results (Phase 4) travel here, so the
# kernel packs them under this bound and the manager never has to reject an oversized
# frame. Matches the manager's SANDBOX_RESULT_BYTES (kernels.py:_RESULT_CAP).
_RESULT_CAP = int(os.environ.get("SANDBOX_RESULT_BYTES", str(2 * 1024 * 1024)))


def _thread_count() -> int:
    """Number of threads in this kernel process. /proc is authoritative — it counts native
    threads a C extension spawned, which ``threading.active_count()`` misses; fall back to the
    Python count if /proc is unreadable. Used to gate the S4 carry-forward (tr-ea8q): a cell
    that left a live thread may write in the idle window, so the next cell must re-scan."""
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("Threads:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return threading.active_count()


def _read_n(fd: int, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = os.read(fd, n - len(buf))
        if not chunk:
            return None  # EOF
        buf += chunk
    return bytes(buf)


def _read_frame(fd: int) -> tuple[bytes, str] | None:
    """Read one code frame ``[4-byte len][8-byte nonce][utf8 code]`` (tr-ea8q). Returns
    ``(nonce, code)``; None on EOF or a frame too short to hold the nonce."""
    hdr = _read_n(fd, 4)
    if hdr is None:
        return None
    (length,) = struct.unpack("<I", hdr)
    body = _read_n(fd, length) if length else b""
    if body is None or len(body) < 8:  # M3: every code frame carries an 8-byte nonce prefix
        return None
    nonce, code = body[:8], body[8:]
    return nonce, code.decode("utf-8", errors="replace")


def _write_result(fd: int, nonce: bytes, obj: dict) -> None:
    # Echo the cell's nonce so the manager can reject a frame forged by a leftover thread.
    data = nonce + json.dumps(obj).encode("utf-8")
    os.write(fd, struct.pack("<I", len(data)) + data)


def main() -> int:
    result_fd = int(os.environ["SANDBOX_RESULT_FD"])
    # Resident resource ceilings, then the network/signal seccomp filter — both
    # installed ONCE, before any user code runs.
    limits.apply_kernel(limits.from_env())
    seccomp.apply()

    namespace: dict = {"__name__": "__main__"}
    stdin_fd = sys.stdin.fileno()
    # Captured ONCE, before any cell runs: a cell may os.chdir() away, but generated-file
    # capture must always diff the SAME tree — the kernel's spawn cwd, which is the
    # per-conversation workdir the manager (kernels.py) reads bytes from. Re-reading
    # os.getcwd() after a cell would follow the chdir and break the diff.
    workdir = os.getcwd()
    # S4 (tr-ea8q): scan the workdir ONCE as the initial baseline and carry each cell's AFTER
    # snapshot forward as the next cell's BEFORE — one scan per cell instead of two. Valid
    # only while the prior cell left no live thread (it could write in the idle window); the
    # `rescan_before` gate (audit M1) falls back to a fresh baseline in that case.
    before = richcapture.snapshot_workspace(workdir)
    rescan_before = False
    while True:
        frame = _read_frame(stdin_fd)
        if frame is None:
            return 0  # manager closed stdin -> shut down cleanly
        nonce, code = frame
        if rescan_before:
            before = richcapture.snapshot_workspace(workdir)
        status = "ok"
        results: list = []
        figs: list = []
        display = None
        try:
            # Run the cell; capture a trailing expression (notebook parity), any open
            # matplotlib figures (PNG), and a DataFrame/repr of the trailing value.
            _, value = richcapture.capture_trailing(code, namespace)
            pyplot = sys.modules.get("matplotlib.pyplot")  # None unless the user imported it
            figs = richcapture.capture_figures(pyplot)
            display = richcapture.display_value(value)
        except SystemExit:
            pass  # a user sys.exit() must NOT kill the persistent kernel
        except BaseException:  # noqa: BLE001 - capture everything from user code
            traceback.print_exc()
            status = "error"
        # Surface generated files even when the cell ERRORED (it may have written a file
        # before raising — the file exists on disk and is the user's, audit M6). Figures /
        # trailing display are success-only; the file diff always runs.
        after = richcapture.snapshot_workspace(workdir)
        generated = richcapture.diff_workspace(before, after)
        # Carry this cell's end state forward as the next baseline ONLY when single-threaded;
        # if the cell left a live thread (S2 is NOT used — threads aren't reaped), it could
        # write in the idle window, so re-scan at the next cell's start instead (audit M1).
        if _thread_count() == 1:
            before = after
            rescan_before = False
        else:
            rescan_before = True
        results = richcapture.pack_results(
            figs, display, _RESULT_CAP, files=richcapture.file_results(generated)
        )
        # Flush user output to fd1/fd2 BEFORE signalling completion, so the manager
        # has all of it once it sees the frame.
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            _write_result(result_fd, nonce, {"status": status, "results": results})
        except OSError:
            # The cell (or a thread it left) closed/broke the result fd (audit FU-1): exit
            # cleanly with a non-zero code instead of an uncaught traceback. The manager sees
            # the kernel die and resets the conversation.
            return 1


if __name__ == "__main__":
    sys.exit(main())
