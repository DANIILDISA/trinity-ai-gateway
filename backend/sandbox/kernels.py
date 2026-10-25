"""Persistent-kernel registry + per-conversation routing (epic tr-s3y1, Phase 3, tr-gbpc).

One long-lived sandbox.kernel child per conversation_id, so variables/imports persist
across executions. The manager talks to each kernel over inherited pipes (NO sockets —
the seccomp network block stays intact): it sends a [len][code] frame on the kernel's
stdin, then drains the kernel's stdout/stderr (capped) while watching a dedicated result
fd for the small completion frame. A per-execution wall-clock deadline bounds each run;
on overrun (or kernel death) the kernel is group-killed and dropped, which resets the
conversation — the next execution lazily spawns a fresh kernel.

Lifecycle (idle eviction + LRU cap) is layered on in tr-mj57.
"""
import base64
import collections
import errno
import fcntl
import json
import logging
import mimetypes
import os
import select
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass

_log = logging.getLogger(__name__)

_HEADER = struct.Struct("<I")
_CHUNK = 64 * 1024
_TRUNCATION_MARKER = "\n…[вывод обрезан]"
# Hard ceiling on a declared completion-frame length BEFORE allocating, so a corrupted
# kernel can't claim a huge frame and OOM the manager (Phase 4 rich results travel here,
# under this bound). Configurable via env; small default is plenty for Phase 3.
_RESULT_CAP = int(os.environ.get("SANDBOX_RESULT_BYTES", str(2 * 1024 * 1024)))


class KernelPoolExhausted(Exception):
    """No free sandbox uid to assign a new kernel. Surfaced by the registry as a
    graceful overload error (reset=False), never an unhandled 500."""


def parse_uid_pool(spec: str | None) -> "collections.deque | None":
    """Parse an inclusive uid-range spec ("60000-60031") into a deque, or None when
    unset/blank. None DISABLES the per-conversation uid drop (the default for the
    backend test container + non-Linux dev), preserving Phase 1-4 behaviour exactly."""
    if not spec or not spec.strip():
        return None
    lo, hi = (int(p) for p in spec.split("-", 1))
    return collections.deque(range(lo, hi + 1))


def isolate_kwargs(uid: int | None, workdir: str) -> tuple[dict, dict]:
    """(env_additions, Popen kwargs) that irreversibly drop a child to ``uid`` and
    confine its temp + matplotlib writes inside ``workdir`` (already chown'd to uid,
    mode 0700). Empty dicts when uid is None (no drop).

    Uses the C-level user=/group=/extra_groups= params (Python 3.9+), NOT a preexec_fn:
    CPython runs setgroups([])→setgid→setuid in the forked child in the correct order,
    async-signal-safe — sidestepping the documented preexec_fn fork-deadlock hazard in
    this multithreaded manager. extra_groups=() is load-bearing: omit it and the child
    keeps root's supplementary groups."""
    if uid is None:
        return {}, {}
    tmp = os.path.join(workdir, ".tmp")
    env = {"HOME": workdir, "TMPDIR": tmp, "MPLCONFIGDIR": os.path.join(tmp, ".mpl")}
    return env, {"user": uid, "group": uid, "extra_groups": ()}


def prepare_uid_workdir(workdir: str, uid: int) -> None:
    """Hand ``workdir`` to the exec uid: chown it (manager holds CAP_CHOWN) and
    pre-create the .tmp/.mpl cache dirs INSIDE it, owned 0700 by the uid — so
    tempfile/matplotlib write there, never to the shared /tmp and never into the
    diff-able workdir root. mkdtemp already made ``workdir`` mode 0700, so chowning
    preserves 0700 with no chmod (which would otherwise need CAP_FOWNER)."""
    os.chown(workdir, uid, uid)
    tmp = os.path.join(workdir, ".tmp")
    mpl = os.path.join(tmp, ".mpl")
    os.mkdir(tmp, 0o700)
    os.chown(tmp, uid, uid)
    os.mkdir(mpl, 0o700)
    os.chown(mpl, uid, uid)


def _proc_ruid(pid) -> "int | None":
    """Real uid of /proc/<pid> (first field of the Uid: line), or None if unreadable."""
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("Uid:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def _live_ruids() -> set:
    """Real uids of all NON-zombie processes, collected in ONE /proc pass. Gates
    reclaiming leaked uids: a uid is reusable only once no live process owns it, else two
    tenants could share it (the breach this scheme exists to prevent)."""
    live = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/status") as fh:
                ruid, zombie = None, False
                for line in fh:
                    if line.startswith("Uid:"):
                        ruid = int(line.split()[1])
                    elif line.startswith("State:"):
                        zombie = line.split()[1] == "Z"
            if ruid is not None and not zombie:
                live.add(ruid)
        except (OSError, ValueError):
            continue
    return live


def _uid_has_live_procs(uid: int) -> bool:
    """True if any non-zombie process currently has real-uid == ``uid``."""
    return uid in _live_ruids()


def kill_process_group(proc, uid) -> None:
    """SIGKILL ``proc``'s whole process group, RECYCLE-SAFE. While the leader is alive it
    holds the pgid (== its pid, via start_new_session), so os.killpg is safe. But once the
    leader is reaped (poll() reaps it) its pid may be RECYCLED by the OS — and with
    CAP_KILL a blind os.killpg of a recycled pid could kill a DIFFERENT tenant's group. So
    when a uid is set and the leader is gone we kill only processes that are BOTH in the
    saved pgid AND owned by that uid (a recycled pid belongs to a different uid). With no
    uid (dev/tests, shared uid) we keep the simple killpg."""
    pgid = proc.pid
    if proc.poll() is None or uid is None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        return
    for entry in os.listdir("/proc"):  # reaped leader → targeted, recycle-safe sweep
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            if os.getpgid(pid) != pgid:
                continue
        except OSError:
            continue
        if _proc_ruid(pid) != uid:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue


# Generated-file byte reader (Phase 5, tr-an0g.3) ------------------------------------
# Caps on the BYTES the manager reads out of a workdir and base64s into the HTTP
# ExecuteResult (which, unlike the completion frame, is NOT bounded by
# SANDBOX_RESULT_BYTES). Env-tunable; the per-file cap matches RLIMIT_FSIZE so any single
# file the sandbox could legitimately write is readable whole.
_FILE_READ_CAP = int(os.environ.get("SANDBOX_FILE_BYTES", str(16 * 1024 * 1024)))
_FILE_TOTAL_CAP = int(os.environ.get("SANDBOX_FILE_TOTAL_BYTES", str(32 * 1024 * 1024)))
_MAX_FILES = int(os.environ.get("SANDBOX_MAX_GENERATED_FILES", "32"))
_FILES_DROPPED_NOTICE = {
    "type": "text/plain",
    "data": "…[часть файлов не возвращена: превышен лимит или недопустимый путь]",
}
# Only these result types may cross the manager trust boundary (audit S1): the completion
# frame is forgeable, so a forged {type:'<anything>'} would otherwise flow verbatim to the
# DB + SSE. The frontend already escapes/sanitises text/html (DOMParser→textContent) and
# text — this is defence-in-depth at the boundary.
_ALLOWED_RESULT_TYPES = frozenset({"image/png", "text/plain", "text/html", "file"})
# Generated files are served back with a NON-EXECUTABLE media type only (audit B1): a
# *.html / *.svg / *.js otherwise downloads with an executable content type and runs when
# opened from the OS file manager (svg+<script> is the worst). Anything outside this set
# (incl. xlsx/parquet/zip) → octet-stream (inert bytes). Mirrors code_exec.media_type_for.
_SAFE_MEDIA_TYPES = frozenset(
    {
        "text/plain", "text/csv", "application/json", "application/pdf",
        "image/png", "image/jpeg", "image/gif", "image/webp",
    }
)


def _safe_media_type(filename: str) -> str:
    guessed = mimetypes.guess_type(filename)[0]
    return guessed if guessed in _SAFE_MEDIA_TYPES else "application/octet-stream"


def _sanitize_name(name: str) -> str:
    """Strip Unicode control/format/surrogate chars (C0 + C1 + BiDi overrides like U+202E,
    audit M1/M4) so nothing contaminates a header or spoofs the displayed extension."""
    return "".join(ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))


def _dedup_name(name: str, seen: dict) -> str:
    """Disambiguate a download basename already used in this result set (L2, tr-dtc8):
    ``data.csv`` → ``data(1).csv`` → ``data(2).csv``, so two generated files with the same
    basename in different workdir subdirs don't overwrite each other in the browser's
    Downloads. ``seen`` maps a name to the next suffix index to try."""
    if name not in seen:
        seen[name] = 1
        return name
    root, ext = os.path.splitext(name)
    n = seen[name]
    while f"{root}({n}){ext}" in seen:
        n += 1
    cand = f"{root}({n}){ext}"
    seen[name] = n + 1
    seen[cand] = 1
    return cand


def _read_workdir_file(base_real: str, rel_path: str, per_file_cap: int) -> "tuple[bytes, str] | None":
    """Securely read a workdir-relative file → (bytes, CANONICAL workdir-relative name), or
    None on ANY rejection. The path is attacker-controlled (untrusted code can forge the
    kernel's completion frame via SANDBOX_RESULT_FD), so every read is hardened:
      - a NUL byte is rejected up front — realpath would raise ValueError (not OSError),
        which uncaught forces a kernel reset (audit M2);
      - realpath must resolve strictly UNDER ``base_real`` — blocks ../ and any symlinked
        parent escaping to /etc, /proc, or a sibling tenant's 0700 workdir;
      - O_NOFOLLOW refuses a symlinked final component (ELOOP); O_NONBLOCK makes a FIFO
        open instantly instead of parking the manager thread waiting for a writer;
      - fstat S_ISREG refuses a FIFO / device / directory; st_size over the cap is dropped
        BEFORE reading (no wasted I/O) and never truncated (half a CSV is useless);
      - a size CHANGE across the read means a concurrent in-process thread mutated the file
        mid-read (audit S2) → the bytes are inconsistent → drop.
    The returned name is the canonical relpath (collapses a/../b → b, dedup; audit M1).
    seccomp already blocks the cell from creating symlinks/FIFOs/devices, so this is
    defence-in-depth against a forged frame and any leftover nodes."""
    if "\x00" in rel_path:
        return None
    try:
        candidate = os.path.realpath(os.path.join(base_real, rel_path))
    except (OSError, ValueError):
        return None
    if candidate != base_real and not candidate.startswith(base_real + os.sep):
        return None
    try:
        fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > per_file_cap:
            return None
        buf = bytearray()
        while len(buf) <= per_file_cap:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            buf += chunk
        if os.fstat(fd).st_size != st.st_size:
            return None  # raced by a concurrent writer thread → inconsistent bytes
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    if len(buf) > per_file_cap:
        return None
    # Return the bytearray directly (audit M7): base64.b64encode + len() accept it via the
    # buffer protocol, saving a full-file bytes() copy.
    return buf, os.path.relpath(candidate, base_real)


def attach_file_bytes(workdir, results, *, per_file_cap=None, total_cap=None, max_files=None):
    """Replace each {type:'file', path} metadata entry (emitted by the kernel's workspace
    diff, tr-an0g.2) with {type:'file', filename, data(base64), media_type, size} read off
    ``workdir``; pass every other ALLOWLISTED result through unchanged. A file that fails
    validation or would push past the per-file / per-cell / count caps, and any result whose
    type is not allowlisted, is dropped; a single RU notice is appended so the user knows
    the list was trimmed. The result filename is the canonical name with ASCII control chars
    stripped (audit M1: no CRLF/control byte reaches Artifact.filename / a header). Returns a
    NEW list."""
    per_file_cap = _FILE_READ_CAP if per_file_cap is None else per_file_cap
    total_cap = _FILE_TOTAL_CAP if total_cap is None else total_cap
    max_files = _MAX_FILES if max_files is None else max_files
    base_real = os.path.realpath(workdir)
    out: list = []
    total = 0
    n_files = 0
    dropped = False
    seen_names: dict = {}  # L2: dedup download basenames within this result set
    for r in results:
        if not (isinstance(r, dict) and r.get("type") == "file"):
            # S1: only allowlisted result types cross the boundary.
            if isinstance(r, dict) and r.get("type") in _ALLOWED_RESULT_TYPES:
                out.append(r)
            else:
                dropped = True
            continue
        if not isinstance(r.get("path"), str) or n_files >= max_files:
            dropped = True
            continue
        read = _read_workdir_file(base_real, r["path"], per_file_cap)
        if read is None or total + len(read[0]) > total_cap:
            dropped = True
            continue
        data, canonical = read
        # L2: the download name is the basename (the subdir path is irrelevant to the user and
        # two same-basename files would otherwise both save as the same name), deduped within
        # the set; M1/M4: strip control + format chars first.
        safe_name = _sanitize_name(os.path.basename(canonical))
        if not safe_name:
            dropped = True
            continue
        safe_name = _dedup_name(safe_name, seen_names)
        total += len(data)
        n_files += 1
        out.append(
            {
                "type": "file",
                "filename": safe_name,
                "data": base64.b64encode(data).decode("ascii"),
                "media_type": _safe_media_type(safe_name),  # B1: non-executable allowlist
                "size": len(data),
            }
        )
    if dropped:
        out.append(dict(_FILES_DROPPED_NOTICE))
    return out


# Upload seeding (Phase 5, tr-an0g.4) ------------------------------------------------
# The backend forwards the conversation's included chat_files; the manager writes them
# into workdir/inputs/ before the cell. Caps bound the shared 64m /workspace tmpfs (the
# pass-1 audit lowered the total from 48MB to 24MB for exactly this reason).
_SEED_FILE_CAP = int(os.environ.get("SANDBOX_SEED_FILE_BYTES", str(16 * 1024 * 1024)))
_SEED_TOTAL_CAP = int(os.environ.get("SANDBOX_SEED_TOTAL_BYTES", str(24 * 1024 * 1024)))


def _safe_input_name(name) -> str | None:
    """Reduce a (semi-trusted) upload filename to a safe inputs/ basename, or None to
    reject. Strips any directory part so a hostile name can't traverse out of inputs/;
    rejects empty / '.' / '..' / a NUL byte (pass-1 audit fold-in)."""
    if not isinstance(name, str):
        return None
    base = os.path.basename(name)
    if not base or base in (".", "..") or "\x00" in base:
        return None
    # Reject names whose UTF-8 encoding exceeds the 255-BYTE filesystem limit (audit M1):
    # truncating could split a multibyte char, and a Cyrillic name >255 bytes (~85 chars)
    # would otherwise hit ENAMETOOLONG at os.open and be silently dropped — routine on the
    # Russian market.
    if len(base.encode("utf-8")) > 255:
        return None
    return base


def _inputs_intact(workdir) -> bool:
    """True iff workdir/inputs is the manager's own root-owned seed dir (audit SB-3, tr-f63e).
    The dropped kernel OWNS its workdir (0700), so it can rename the root-owned 0555 inputs/
    out of the way and substitute a uid-owned dir of its choosing — read-only files alone do
    not make inputs/ tamper-proof. The substitute is necessarily uid-owned (the kernel has no
    CAP_CHOWN to forge root ownership), so a non-root owner (or a missing dir) means tampered:
    a probe-match must then force a re-seed instead of trusting the in-memory _seeded_hash."""
    try:
        st = os.stat(os.path.join(workdir, "inputs"))
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == 0


def seed_inputs(workdir, files, uid=None, *, per_file_cap=None, total_cap=None) -> list[str]:
    """(Re)create ``workdir``/inputs and write the included upload files into it BEFORE the
    cell runs, so code can read ``inputs/<name>``. Cleaned + re-seeded every call so the
    inputs always reflect the current context and never accumulate. Per-uid: chown the dir
    + files to the exec uid (manager holds CAP_CHOWN) so the dropped kernel can read its own
    0700 inputs. Each write is O_CREAT|O_EXCL|O_NOFOLLOW into the freshly-made dir, so a
    pre-existing node can't redirect it. Oversized / over-budget / unsafe / undecodable
    entries are skipped. Returns the basenames actually seeded."""
    per_file_cap = _SEED_FILE_CAP if per_file_cap is None else per_file_cap
    total_cap = _SEED_TOTAL_CAP if total_cap is None else total_cap
    inputs = os.path.join(workdir, "inputs")
    # Always clear stale inputs first (e.g. the user excluded a previously-seeded file);
    # only materialise the dir when there is something to seed, so a no-upload cell does
    # no needless work and leaves no empty inputs/ behind.
    shutil.rmtree(inputs, ignore_errors=True)
    if not files:
        return []
    # M4 (tr-f63e): inputs/ is seeded READ-ONLY and root-owned (0555 dir / 0444 files). The
    # dropped kernel reads via the 'other' permission bits but cannot modify, delete, or add
    # entries (no write on the dir). This keeps inputs/ DB-authoritative even though B3 no
    # longer rebuilds it every cell on a probe-match. `uid` is now unused for ownership.
    try:
        os.mkdir(inputs, 0o555)  # FU-3: ≤0555 from the start (umask-proof, no world-writable window)
    except FileExistsError:
        # L3 (tr-dtc8): a leftover bg thread re-created inputs/ between the rmtree above and
        # here — clear it and retry once rather than let EEXIST bubble up and silently skip
        # seeding (Kernel.execute would swallow the OSError). Defence-in-depth: if the node is
        # a SYMLINK, rmtree(ignore_errors) would leave it and the retry mkdir would follow it
        # to its target — unlink it explicitly first (seccomp blocks symlink() in prod, so this
        # is belt-and-suspenders for non-seccomp environments).
        if os.path.islink(inputs):
            try:
                os.unlink(inputs)
            except OSError:
                pass
        else:
            shutil.rmtree(inputs, ignore_errors=True)
        os.mkdir(inputs, 0o555)
    os.chmod(inputs, 0o555)
    seeded: list[str] = []
    total = 0
    for f in files or []:
        if not isinstance(f, dict):
            continue
        name = _safe_input_name(f.get("filename"))
        if name is None:
            continue
        try:
            data = base64.b64decode(f.get("base64") or "", validate=True)
        except Exception:  # noqa: BLE001 - skip an undecodable payload, never raise
            continue
        if len(data) > per_file_cap or total + len(data) > total_cap:
            continue  # over a cap → skip (tmpfs is small + shared)
        path = os.path.join(inputs, name)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError:
            continue  # duplicate basename (EEXIST) or any open failure → skip
        try:
            os.write(fd, data)
            os.fchmod(fd, 0o444)  # M4: read-only file (untrusted code can read, not tamper)
        except OSError as exc:
            os.close(fd)
            try:
                os.unlink(path)  # M2: don't leave a root-owned orphan in inputs/
            except OSError:
                pass
            if exc.errno == errno.ENOSPC:
                # B2: shared tmpfs is full — make the (otherwise silent) upload loss
                # observable/alertable. M1 (tr-f63e): RAISE so the caller does NOT cache a
                # partial seed under the full seed_hash; the next probe forces a re-seed.
                _log.warning(
                    "sandbox: ENOSPC seeding %r in %s — uploads unavailable to the cell",
                    name, workdir,
                )
                raise
            continue  # other write error → skip this file, keep going
        os.close(fd)
        seeded.append(name)
        total += len(data)
    return seeded


@dataclass
class KernelResult:
    status: str  # ok | error | timeout
    stdout: str
    stderr: str
    truncated: bool
    duration_ms: int
    detail: str | None = None
    reset: bool = False
    results: list | None = None
    needs_seed: bool = False  # B3 (tr-f63e): probe miss — backend retries with full bytes


class _Capped:
    """Accumulates bytes up to `cap`, flagging truncation and discarding the rest —
    but the caller keeps READING the fd (so the kernel never blocks on a full pipe)."""

    def __init__(self, cap: int):
        self.cap = cap
        self._buf = bytearray()
        self.truncated = False

    def feed(self, chunk: bytes) -> None:
        room = self.cap - len(self._buf)
        if room > 0:
            self._buf += chunk[:room]
        if len(chunk) > room:
            self.truncated = True

    def text(self) -> str:
        out = self._buf.decode("utf-8", errors="replace")
        return out + _TRUNCATION_MARKER if self.truncated else out


class Kernel:
    """A single persistent kernel child + its result pipe + a single-flight lock."""

    def __init__(self, conversation_id: str, workdir: str, output_cap: int, uid: int | None = None):
        self.conversation_id = conversation_id
        self.workdir = workdir
        self.uid = uid  # the dropped exec uid (None = no per-conversation drop)
        self._output_cap = output_cap
        self.last_used = time.monotonic()
        self._seeded_hash: str | None = None  # B3 (tr-f63e): hash of the set last seeded into inputs/
        self._expected_nonce: bytes | None = None  # tr-ea8q: per-cell nonce the kernel must echo
        self.lock = threading.Lock()  # one execution at a time per kernel
        self._rfd, wfd = os.pipe()
        iso_env, iso_kw = isolate_kwargs(uid, workdir)
        try:
            self.proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                [sys.executable, "-I", "-m", "sandbox.kernel"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=workdir,
                start_new_session=True,  # own group → group-kill on reset/eviction
                pass_fds=(wfd,),
                env={**os.environ, "SANDBOX_RESULT_FD": str(wfd), **iso_env},
                **iso_kw,  # drop to `uid` (C-level user=/group=/extra_groups=) when set
            )
        except BaseException:
            # Spawn failed (e.g. fork hit pids_limit) — don't leak the pipe fds in
            # the long-lived manager process (would exhaust RLIMIT_NOFILE over time).
            os.close(self._rfd)
            os.close(wfd)
            raise
        os.close(wfd)  # success: the child holds the write end
        for f in (self.proc.stdout, self.proc.stderr):
            fl = fcntl.fcntl(f.fileno(), fcntl.F_GETFL)
            fcntl.fcntl(f.fileno(), fcntl.F_SETFL, fl | os.O_NONBLOCK)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def rss_kb(self) -> int:
        """Resident set size of the kernel process in KiB (0 if unreadable). Phase 4:
        the DS stack makes per-kernel RSS the binding memory constraint, so the
        registry sums this to decide RSS-based eviction."""
        try:
            with open(f"/proc/{self.proc.pid}/status") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1])
        except (OSError, ValueError):
            pass
        return 0

    def execute(self, code: str, wall_seconds: int, files=None, seed_hash=None) -> tuple[KernelResult, bool]:
        """Run one frame. Returns (KernelResult, dead) — dead=True means the kernel
        must be dropped (timeout or crash) and the conversation reset."""
        start = time.monotonic()
        # Seed the conversation's included upload files into workdir/inputs/ BEFORE the
        # cell, so code reads inputs/<name> (Phase 5.4). Cleaned + re-seeded per call. The
        # kernel is idle here (waiting on stdin) and holds its single-flight lock, so no
        # user code races the writes. Best-effort: a seeding hiccup leaves the cell to hit
        # FileNotFoundError and self-correct, never crashes the run.
        # B3 (tr-f63e): seed inputs/ only when the set changed. files==[] AND a seed_hash =
        # PROBE: run as-is on a match (inputs/ already correct, same locked call → no eviction
        # window), else answer needs_seed so the backend retries with full bytes. A fresh or
        # evicted kernel has _seeded_hash=None → miss.
        if not files and seed_hash is not None:
            if self._seeded_hash != seed_hash or not _inputs_intact(self.workdir):
                # Miss, OR the cell tampered with inputs/ since it was seeded (audit SB-3) →
                # don't trust the cached hash; make the backend re-seed from the DB.
                dead = not self.alive()  # N6: drop a dead kernel now, free its uid a round-trip earlier
                return (
                    KernelResult(
                        status="error", stdout="", stderr="", truncated=False,
                        duration_ms=0, needs_seed=True,
                    ),
                    dead,
                )
            # match → skip re-seeding, fall through and run the cell
        else:
            try:
                seed_inputs(self.workdir, files, self.uid)
                self._seeded_hash = seed_hash
            except OSError:
                # M1: a partial/failed seed (e.g. ENOSPC) must not be cached under seed_hash —
                # force a full re-seed on the next probe.
                self._seeded_hash = None
        out, err = _Capped(self._output_cap), _Capped(self._output_cap)
        # Drain any STALE bytes a prior cell's leftover thread may have written to the result
        # pipe (audit FU-2): the kernel is idle (blocked on stdin under our lock), so anything
        # readable now predates this cell and must not be misparsed as this cell's frame —
        # which would nonce-mismatch and spuriously reset a correct cell.
        while select.select([self._rfd], [], [], 0)[0]:
            try:
                if not os.read(self._rfd, _CHUNK):
                    break
            except (BlockingIOError, OSError):
                break
        # tr-ea8q: stamp each cell with a fresh unpredictable nonce the kernel echoes in its
        # completion frame, so a leftover thread that writes to the result fd can't forge a
        # frame the manager will accept. MUST be a CSPRNG — a predictable PRNG (random/Mersenne
        # Twister) would let the thread compute the nonce and defeat the defense.
        nonce = os.urandom(8)
        self._expected_nonce = nonce
        cb = code.encode("utf-8")
        try:
            self.proc.stdin.write(_HEADER.pack(8 + len(cb)) + nonce)  # header + nonce
            self.proc.stdin.write(cb)                                 # then code (no concat copy)
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            return self._fail("Ядро недоступно", start), True

        out_fd, err_fd = self.proc.stdout.fileno(), self.proc.stderr.fileno()
        rbuf = bytearray()
        frame: dict | None = None
        timed_out = False
        deadline = start + wall_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([out_fd, err_fd, self._rfd], [], [], min(remaining, 0.1))
            for fd in ready:
                try:
                    chunk = os.read(fd, _CHUNK)
                except (BlockingIOError, OSError):
                    continue
                if not chunk:
                    continue
                if fd == out_fd:
                    out.feed(chunk)
                elif fd == err_fd:
                    err.feed(chunk)
                else:
                    rbuf += chunk
            frame = self._try_parse(rbuf)
            if frame is not None:
                break
            if self.proc.poll() is not None:  # kernel died without a frame
                break

        if timed_out:
            return self._timeout(wall_seconds, start), True
        if frame is None:  # crash / oversized frame
            return self._fail("Ядро завершилось аварийно", start), True

        self._final_drain(out_fd, err_fd, out, err)
        if self._has_group_children():
            # The cell left LIVE child processes (e.g. a fork-bomb that filled
            # pids_limit, so the kernel caught the fork error and survived). Those
            # children share the kernel's stdin/result fd and would corrupt the
            # protocol + exhaust pids. Reset the whole kernel: dead=True → the
            # registry group-kills it, reaping every child, and the next call spawns
            # a clean kernel.
            return (
                KernelResult(
                    status="error",
                    stdout=out.text(),
                    stderr=err.text(),
                    truncated=out.truncated or err.truncated,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    detail="Код оставил активные дочерние процессы — сессия сброшена.",
                    reset=True,
                ),
                True,
            )
        self.last_used = time.monotonic()
        # Read the bytes of any generated-file metadata off this kernel's workdir (Phase
        # 5.3). Runs AFTER the cell (kernel idle, single-flight lock held) and AFTER
        # _has_group_children, so no user code is concurrently racing the reader; the path
        # is still treated as untrusted (forged-frame defence lives in attach_file_bytes).
        frame_results = frame["results"] if isinstance(frame.get("results"), list) else []
        return (
            KernelResult(
                status="ok" if frame.get("status") == "ok" else "error",
                stdout=out.text(),
                stderr=err.text(),
                truncated=out.truncated or err.truncated,
                duration_ms=int((time.monotonic() - start) * 1000),
                results=attach_file_bytes(self.workdir, frame_results),
            ),
            False,
        )

    def _has_group_children(self) -> bool:
        """True if the kernel's process group has members other than the kernel
        itself — i.e. the last cell forked processes that are still alive."""
        try:
            pgid = os.getpgid(self.proc.pid)
        except OSError:
            return False
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == self.proc.pid:
                continue
            try:
                if os.getpgid(pid) == pgid:
                    return True
            except OSError:
                continue
        return False

    def _try_parse(self, rbuf: bytearray) -> dict | None:
        if len(rbuf) < 4:
            return None
        (length,) = _HEADER.unpack(rbuf[:4])
        if length > _RESULT_CAP:
            raise ValueError("oversized completion frame")  # → treated as crash → reset
        if len(rbuf) < 4 + length:
            return None
        if length < 8:
            raise ValueError("completion frame missing nonce")  # → reset
        # tr-ea8q: the frame must carry the per-cell nonce we sent. A leftover thread that
        # wrote to the result fd can't know the random nonce, so a mismatch = a forged or
        # desynced frame — never accept it as this cell's result. (The KernelRegistry.execute
        # blanket `except Exception` turns this ValueError into a clean conversation reset.)
        nonce = bytes(rbuf[4:12])
        if nonce != self._expected_nonce:
            _log.warning(
                "sandbox: result-frame nonce mismatch cid=%s — resetting "
                "(possible leftover-thread forge)", self.conversation_id,
            )
            raise ValueError("completion frame nonce mismatch")
        frame = json.loads(bytes(rbuf[12 : 4 + length]).decode("utf-8", errors="replace"))
        # Validate the frame shape: untrusted code (the result fd is in its env) could
        # forge a frame. A malformed one raises → treated as a crash → conversation
        # reset (never a 500). Cross-tenant safe: the fd is per-conversation.
        if not isinstance(frame, dict) or frame.get("status") not in ("ok", "error"):
            raise ValueError("malformed completion frame")
        return frame

    def _final_drain(self, out_fd, err_fd, out, err) -> None:
        # The kernel flushes fd1/fd2 before the completion frame; grab any stragglers.
        end = time.monotonic() + 0.1
        while time.monotonic() < end:
            ready, _, _ = select.select([out_fd, err_fd], [], [], 0.02)
            if not ready:
                break
            for fd in ready:
                try:
                    chunk = os.read(fd, _CHUNK)
                except (BlockingIOError, OSError):
                    chunk = b""
                if chunk:
                    (out if fd == out_fd else err).feed(chunk)

    def _timeout(self, wall_seconds, start) -> KernelResult:
        return KernelResult(
            status="timeout", stdout="", stderr="", truncated=False,
            duration_ms=int((time.monotonic() - start) * 1000),
            detail=f"Превышен лимит времени ({wall_seconds}с)", reset=True,
        )

    def _fail(self, detail, start) -> KernelResult:
        return KernelResult(
            status="error", stdout="", stderr="", truncated=False,
            duration_ms=int((time.monotonic() - start) * 1000), detail=detail, reset=True,
        )

    def close(self) -> bool:
        """Tear the kernel down. Returns True only when FULLY reaped — the process was
        waited AND the workdir removed — so the registry can safely recycle its uid. A
        False return (wait timed out, or the 0700 workdir could not be removed) means
        the uid must be LEAKED, not reused, until /proc confirms no live holder.

        Recycling is otherwise safe because setsid/setpgid are seccomp-blocked: a cell
        cannot leave the kernel's process group, so killpg+wait reaps every descendant
        (init:true sweeps any straggler) — no same-uid escapee can outlive the kill."""
        reaped = True
        kill_process_group(self.proc, self.uid)
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            reaped = False
        for f in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                f.close()
            except (ValueError, OSError):
                pass
        try:
            os.close(self._rfd)
        except OSError:
            pass
        if self.uid is None:
            shutil.rmtree(self.workdir, ignore_errors=True)
        else:
            # Manager (root + CAP_DAC_OVERRIDE/FOWNER) can remove the uid-owned 0700
            # tree; surface a failure so the uid is leaked-not-reused, not lost silently.
            try:
                shutil.rmtree(self.workdir)
            except OSError as exc:
                _log.warning("sandbox: rmtree of uid %s workdir %s failed, leaking: %r",
                             self.uid, self.workdir, exc)
                reaped = False
        return reaped


class KernelRegistry:
    def __init__(
        self,
        workdir_base: str | None,
        output_cap: int,
        max_kernels: int,
        idle_seconds: float = 600.0,
        uid_pool=None,
    ):
        self._kernels: dict[str, Kernel] = {}
        self._lock = threading.Lock()
        self._workdir_base = workdir_base
        self._output_cap = output_cap
        self._max_kernels = max_kernels
        self._idle_seconds = idle_seconds
        # Phase 4: evict free LRU kernels once the live set's total RSS crosses this
        # (KiB); 0 disables RSS-based eviction (count cap only). See _over_rss_budget.
        self._rss_evict_kb = int(os.environ.get("SANDBOX_RSS_EVICT_KB", "0"))
        # Per-conversation uid isolation (tr-an0g.7). None → no uid drop (tests/dev).
        self._uid_pool = collections.deque(uid_pool) if uid_pool is not None else None
        # uids released WITHOUT a confirmed reap, mapped to their surviving workdir; the
        # reaper reclaims a uid once /proc shows no live holder and removes the leftover
        # workdir (leak-then-reclaim — never reuse a uid with a live owner).
        self._leaked_uids: dict[int, str] = {}

    def _spawn(self, conversation_id: str) -> Kernel:
        # Runs under self._lock (called from _get), so the pool ops here are serialized.
        uid = None
        if self._uid_pool is not None:
            try:
                uid = self._uid_pool.popleft()
            except IndexError:
                raise KernelPoolExhausted("no free sandbox uid")
        workdir = tempfile.mkdtemp(dir=self._workdir_base, prefix="k-")
        try:
            if uid is not None:
                prepare_uid_workdir(workdir, uid)
            return Kernel(conversation_id, workdir, self._output_cap, uid)
        except BaseException:
            # Kernel construction failed → don't orphan the workdir (the stateless
            # path's TemporaryDirectory auto-cleaned; we mkdtemp manually here). No
            # process ever ran as `uid`, so it is safe to recycle immediately.
            shutil.rmtree(workdir, ignore_errors=True)
            if uid is not None:
                self._uid_pool.append(uid)
            raise

    def _close_and_release(self, kernel: Kernel) -> None:
        """Close a kernel ALREADY removed from self._kernels (the caller that popped
        it owns this → exactly-once). Append its uid back to the pool only after a
        confirmed reap; otherwise leak it for the reaper to reclaim. Runs OUTSIDE
        self._lock (close() does a slow killpg+wait); the append re-takes the lock
        briefly. NEVER call this from within a `with self._lock` block (the Lock is
        non-reentrant) — except the _spawn failure-path append, which is a bare
        deque.append with no close()."""
        reaped = kernel.close()
        if kernel.uid is None or self._uid_pool is None:
            return
        with self._lock:
            if reaped:
                self._uid_pool.append(kernel.uid)
            else:
                self._leaked_uids[kernel.uid] = kernel.workdir

    def _reclaim_leaked_uids(self) -> int:
        """Return leaked uids to the pool once /proc shows no live process owns them, and
        remove their leftover workdirs (the manager's CAP_DAC_OVERRIDE/FOWNER lets rmtree
        of the uid-owned 0700 tree succeed on retry). ONE /proc pass regardless of how
        many uids are pending; a no-op unless uids are actually pending."""
        if self._uid_pool is None:
            return 0
        with self._lock:
            pending = dict(self._leaked_uids)
        if not pending:
            return 0
        live = _live_ruids()
        freed = [uid for uid in pending if uid not in live]
        for uid in freed:
            shutil.rmtree(pending[uid], ignore_errors=True)
        with self._lock:
            for uid in freed:
                if uid in self._leaked_uids:
                    del self._leaked_uids[uid]
                    self._uid_pool.append(uid)
        return len(freed)

    def _lru_victim_locked(self) -> str | None:
        """Least-recently-used conversation whose kernel is NOT mid-execution
        (its single-flight lock is free). None when every kernel is busy — we then
        tolerate a brief over-cap rather than kill a running execution."""
        free = [(k.last_used, cid) for cid, k in self._kernels.items() if not k.lock.locked()]
        if not free:
            return None
        free.sort()
        return free[0][1]

    def _over_rss_budget(self) -> bool:
        """True when RSS eviction is enabled and the live kernels' summed RSS is at or
        over the threshold — caller then evicts the LRU free kernel and re-checks."""
        if self._rss_evict_kb <= 0 or not self._kernels:
            return False
        return sum(k.rss_kb() for k in self._kernels.values()) >= self._rss_evict_kb

    def _get(self, conversation_id: str) -> Kernel:
        try:
            return self._get_inner(conversation_id)
        except KernelPoolExhausted:
            # _get_inner's finally already returned its evicted victims' uids to the pool;
            # also reclaim any leaked uids, then retry ONCE before surfacing overload —
            # avoids a false 503 while a freed uid waits up to a full reaper cycle.
            self._reclaim_leaked_uids()
            return self._get_inner(conversation_id)

    def _get_inner(self, conversation_id: str) -> Kernel:
        to_close: list[Kernel] = []
        try:
            with self._lock:
                kernel = self._kernels.get(conversation_id)
                if kernel is not None and not kernel.alive():
                    to_close.append(self._kernels.pop(conversation_id))
                    kernel = None
                if kernel is None:
                    # Evict free LRU kernels while at the residency cap OR over the RSS
                    # budget (skip busy kernels — _lru_victim_locked returns None then).
                    while len(self._kernels) >= self._max_kernels or self._over_rss_budget():
                        victim = self._lru_victim_locked()
                        if victim is None:
                            break
                        to_close.append(self._kernels.pop(victim))
                    kernel = self._spawn(conversation_id)
                    self._kernels[conversation_id] = kernel
                else:
                    kernel.last_used = time.monotonic()
        finally:
            # Close evicted/dead victims OUTSIDE the lock (kill+wait is slow) and ALWAYS,
            # even if _spawn raised — else the popped victims leak process+workdir+uid.
            for dead in to_close:
                self._close_and_release(dead)
        return kernel

    def reap_idle(self) -> int:
        """Evict kernels idle past idle_seconds (skip any mid-execution). Returns
        the number reaped. Called periodically by the reaper thread."""
        now = time.monotonic()
        to_close: list[Kernel] = []
        with self._lock:
            for cid in list(self._kernels):
                k = self._kernels[cid]
                if not k.lock.locked() and (now - k.last_used) > self._idle_seconds:
                    to_close.append(self._kernels.pop(cid))
        for dead in to_close:
            self._close_and_release(dead)
        return len(to_close)

    def start_reaper(self, interval: float = 30.0) -> None:
        def _loop() -> None:
            while True:
                time.sleep(interval)
                try:
                    self.reap_idle()
                    self._reclaim_leaked_uids()
                except Exception:  # noqa: BLE001 - the reaper must never die
                    pass

        threading.Thread(target=_loop, daemon=True, name="kernel-reaper").start()

    def _drop(self, conversation_id: str, kernel: Kernel) -> None:
        # Only the caller that OWNS the dict entry may close+release — a stale _drop on
        # a kernel another path already evicted would double-release its uid (two future
        # kernels sharing one uid = cross-tenant breach, the thing this scheme prevents).
        with self._lock:
            owned = self._kernels.get(conversation_id) is kernel
            if owned:
                self._kernels.pop(conversation_id, None)
        if owned:
            self._close_and_release(kernel)

    def execute(self, conversation_id: str, code: str, wall_seconds: int, files=None, seed_hash=None) -> KernelResult:
        try:
            kernel = self._get(conversation_id)
        except KernelPoolExhausted:
            return KernelResult(
                status="error", stdout="", stderr="", truncated=False,
                duration_ms=0, detail="Песочница перегружена, попробуйте позже", reset=False,
            )
        with kernel.lock:  # single-flight per conversation
            try:
                result, dead = kernel.execute(code, wall_seconds, files, seed_hash)
            except Exception:  # noqa: BLE001 - any protocol/IO failure → reset, never crash
                self._drop(conversation_id, kernel)
                return KernelResult(
                    status="error", stdout="", stderr="", truncated=False,
                    duration_ms=0, detail="Ошибка ядра", reset=True,
                )
        if dead:
            self._drop(conversation_id, kernel)
        return result

    def shutdown(self) -> None:
        with self._lock:
            kernels = list(self._kernels.values())
            self._kernels.clear()
        for kernel in kernels:
            self._close_and_release(kernel)
