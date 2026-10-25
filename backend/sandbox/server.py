"""Sandbox manager HTTP API (epic tr-s3y1, Phase 1).

Minimal FastAPI service — OUR trusted code. POST /execute runs the submitted
code in a FRESH hardened child process (sandbox.runner) inside a throwaway temp
workdir, and returns the captured result; the untrusted code never runs in this
process. Stateless in Phase 1 (a new process per request); per-conversation
kernels arrive in Phase 3 behind the same API.
"""
import fcntl
import os
import select
import subprocess
import sys
import tempfile
import threading
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from sandbox import kernels, limits

app = FastAPI(title="Trinity sandbox", docs_url=None, redoc_url=None)
_LIMITS = limits.from_env()
# Per-execution working directory base. /workspace is a tmpfs in the sandbox
# container; falls back to the system temp dir (also tmpfs there) when absent
# (e.g. the backend test container has no /workspace).
_WORKSPACE_ROOT = os.environ.get("SANDBOX_WORKSPACE_ROOT", "/workspace")
_TRUNCATION_MARKER = "\n…[вывод обрезан]"
# How many bytes to pull per read while draining a child pipe. Bounded so a flood
# can never be slurped into one giant read — we notice we've passed the cap within
# one chunk and stop (tr-lcmd: keep manager RSS bounded, don't buffer the stream).
_READ_CHUNK = 16 * 1024
# select() poll interval — small so a drained reader notices `stop_event` quickly.
_SELECT_TICK = 0.05
# Grace period to collect the last in-flight bytes after the child is group-killed.
_JOIN_TIMEOUT = 2.0
# Global concurrency cap (tr-bzk0): the single container has fixed CPU/RAM/pids, so
# bound how many runs execute at once. Non-blocking acquire → immediate 503 when full.
_MAX_CONCURRENT = int(os.environ.get("SANDBOX_MAX_CONCURRENT", "4"))
_SLOTS = threading.BoundedSemaphore(_MAX_CONCURRENT)
# Persistent per-conversation kernels (Phase 3, tr-gbpc). conversation_id routes
# here; None stays on the stateless _run_execute path (Phase 1/2 parity).
_MAX_KERNELS = int(os.environ.get("SANDBOX_MAX_KERNELS", "8"))
_IDLE_SECONDS = float(os.environ.get("SANDBOX_KERNEL_IDLE_SECONDS", "600"))
# Per-conversation uid isolation (tr-an0g.7). Each sandboxed child drops to a DISTINCT
# uid so one tenant's 0700 workspace files are unreadable to another. Two DISJOINT pools
# (a kernel and a stateless run must never share a uid): kernels route through the
# registry; the stateless conversation_id=None path uses _STATELESS_UID_POOL. Both unset
# → no drop, preserving Phase 1-4 behaviour (backend test container / non-Linux dev).
_KERNEL_UID_POOL = kernels.parse_uid_pool(os.environ.get("SANDBOX_UID_POOL"))
_STATELESS_UID_POOL = kernels.parse_uid_pool(os.environ.get("SANDBOX_STATELESS_UID_POOL"))
_STATELESS_LOCK = threading.Lock()
# Stateless uids released WITHOUT a confirmed reap; reclaimed lazily once /proc shows no
# live holder (mirrors the registry's leak-then-reclaim — never reuse a live-owned uid).
_STATELESS_LEAKED: set[int] = set()


def _check_pools_disjoint(kernel_pool, stateless_pool) -> None:
    """Refuse to start if the kernel and stateless uid ranges overlap — a kernel and a
    stateless run sharing a uid would defeat per-tenant isolation. No-op if either unset."""
    if kernel_pool is None or stateless_pool is None:
        return
    overlap = set(kernel_pool) & set(stateless_pool)
    if overlap:
        raise RuntimeError(
            f"SANDBOX_UID_POOL and SANDBOX_STATELESS_UID_POOL overlap: {sorted(overlap)}"
        )


_check_pools_disjoint(_KERNEL_UID_POOL, _STATELESS_UID_POOL)
_REGISTRY = kernels.KernelRegistry(
    workdir_base=_WORKSPACE_ROOT if os.path.isdir(_WORKSPACE_ROOT) else None,
    output_cap=_LIMITS.output_bytes,
    max_kernels=_MAX_KERNELS,
    idle_seconds=_IDLE_SECONDS,
    uid_pool=_KERNEL_UID_POOL,
)
# Background reaper sweeps idle kernels (daemon thread; no-op until a kernel goes idle).
_REGISTRY.start_reaper()


def _reclaim_stateless_uids() -> None:
    """Return leaked stateless uids to the pool once /proc shows no live holder."""
    if _STATELESS_UID_POOL is None:
        return
    with _STATELESS_LOCK:
        pending = list(_STATELESS_LEAKED)
    if not pending:
        return
    live = kernels._live_ruids()
    with _STATELESS_LOCK:
        for uid in pending:
            if uid not in live and uid in _STATELESS_LEAKED:
                _STATELESS_LEAKED.discard(uid)
                _STATELESS_UID_POOL.append(uid)


def _acquire_stateless_uid() -> int | None:
    """Take a uid for one stateless run, or None when the pool is disabled (no drop).
    On an empty pool, first reclaim any free leaked uid; only then fail CLOSED (raise) —
    never run untrusted code as root just because allocation momentarily failed. _SLOTS
    gates concurrency below the pool size, so this should not be reached in practice."""
    if _STATELESS_UID_POOL is None:
        return None
    with _STATELESS_LOCK:
        if _STATELESS_UID_POOL:
            return _STATELESS_UID_POOL.popleft()
    _reclaim_stateless_uids()  # eager reclaim before failing
    with _STATELESS_LOCK:
        if _STATELESS_UID_POOL:
            return _STATELESS_UID_POOL.popleft()
    raise kernels.KernelPoolExhausted("no free stateless sandbox uid")


def _release_stateless_uid(uid: int | None, reaped: bool = True) -> None:
    """Return a stateless uid to the pool, but ONLY when its child was confirmed reaped;
    otherwise hold it in the leaked set (a live process may still own it) for later
    /proc-gated reclaim, mirroring the kernel-registry invariant."""
    if uid is None or _STATELESS_UID_POOL is None:
        return
    with _STATELESS_LOCK:
        if reaped:
            _STATELESS_UID_POOL.append(uid)
        else:
            _STATELESS_LEAKED.add(uid)


class InputFile(BaseModel):
    """One upload to seed into the kernel workdir's inputs/ (Phase 5.4). filename is
    sanitised to a basename by the manager; base64 is the raw payload. Sizes are bounded at
    the boundary (audit M3) — the manager has no body-size middleware, so reject an oversized
    payload BEFORE it is decoded into memory (~23MB base64 ≈ a 16MB file)."""

    filename: str = Field(max_length=255)
    base64: str = Field(max_length=23_000_000)
    media_type: str | None = Field(default=None, max_length=255)


class ExecuteRequest(BaseModel):
    code: str
    timeout: int | None = None
    # Routes to this conversation's persistent kernel (Phase 3); None = stateless.
    conversation_id: str | None = None
    # Included chat_files to seed into workdir/inputs/ before the cell (Phase 5.4). Count
    # bounded (audit M3) so a forwarded/crafted list can't OOM the manager.
    files: list[InputFile] = Field(default_factory=list, max_length=64)
    # B3 (tr-f63e): when files==[] and seed_hash is set, this is a PROBE — run only if the
    # kernel already seeded this exact hash, else answer needs_seed. Format-validated (the
    # backend sends a sha256 hexdigest) to reject junk on this internal endpoint.
    seed_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ExecuteResult(BaseModel):
    status: str  # ok | error | timeout
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool
    detail: str | None = None
    # Phase 3: the conversation's kernel was reset; Phase 4-ready rich results.
    reset: bool = False
    results: list = Field(default_factory=list)
    # B3 (tr-f63e): probe miss — the backend must retry with full file bytes.
    needs_seed: bool = False


def _set_nonblocking(fd: int) -> None:
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)


def _drain(
    fd: int, cap: int, sink: dict, cap_event: threading.Event, stop_event: threading.Event
) -> None:
    """Read raw bytes from ``fd`` into ``sink['data']`` keeping at most ``cap`` bytes.

    Reads NON-BLOCKING via ``select`` so the loop can re-check ``stop_event`` and
    exit even if the pipe never reaches EOF — e.g. an untrusted child that forked
    and held the inherited write fd open after escaping the process group. A
    blocking ``read()`` would park this thread forever (a leak; and a later
    ``pipe.close()`` would hang the request on the buffered-reader lock). Stops the
    moment MORE than ``cap`` bytes are seen — sets ``sink['truncated']`` and
    ``cap_event`` so the caller can kill the flood instead of buffering it (tr-lcmd:
    bound manager RSS regardless of output volume). Bytes are kept raw and decoded
    once at assembly (errors='replace'), so a binary / non-UTF-8 flood (matplotlib
    PNG, pickle) is still capped and never crashes the reader mid-stream."""
    buf = bytearray()
    total = 0
    try:
        while not stop_event.is_set():
            ready, _, _ = select.select([fd], [], [], _SELECT_TICK)
            if not ready:
                continue
            try:
                chunk = os.read(fd, _READ_CHUNK)
            except BlockingIOError:
                continue
            except OSError:
                break  # fd closed underneath us
            if not chunk:
                break  # EOF — every write end is closed
            buf += chunk
            total += len(chunk)
            if total > cap:
                sink["truncated"] = True
                cap_event.set()
                break  # cap exceeded — stop reading, don't drain the rest
    finally:
        sink["data"] = bytes(buf)


def _assemble(sink: dict, cap: int) -> tuple[str, bool]:
    data: bytes = sink.get("data", b"")
    text = data[:cap].decode("utf-8", errors="replace")
    if sink.get("truncated"):
        return text + _TRUNCATION_MARKER, True
    return text, False


def _kill_group(proc: subprocess.Popen, uid: int | None = None) -> None:
    """SIGKILL the runner's whole process group — the runner AND any descendants it
    forked — RECYCLE-SAFE (see kernels.kill_process_group): with a per-run uid set, a
    reaped+recycled leader pid is never blind-killpg'd (which, under CAP_KILL, could hit a
    different tenant's group). start_new_session=True made the runner the group leader, so
    this reaps a fork bomb / runaway children. (`init: true` reaps double-forked escapees.)
    """
    kernels.kill_process_group(proc, uid)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/execute", response_model=ExecuteResult)
def execute(req: ExecuteRequest) -> ExecuteResult:
    # Shed load immediately (503, no queue) when every concurrency slot is taken,
    # so a burst of runs can't exhaust the container (tr-bzk0).
    if not _SLOTS.acquire(blocking=False):
        raise HTTPException(status_code=503, detail="Песочница перегружена, попробуйте позже")
    try:
        try:
            if req.conversation_id:
                return _execute_in_kernel(req)
            return _run_execute(req)
        except kernels.KernelPoolExhausted:
            # No free sandbox uid for a stateless run — shed gracefully (the kernel
            # path handles its own exhaustion inside the registry).
            return ExecuteResult(
                status="error", stdout="", stderr="", duration_ms=0, truncated=False,
                detail="Песочница перегружена, попробуйте позже",
            )
        except OSError:
            # Spawning the child failed (e.g. fork hit pids_limit under load) — return
            # a graceful tool error, never a 500, so the chat keeps working.
            return ExecuteResult(
                status="error", stdout="", stderr="", duration_ms=0, truncated=False,
                detail="Не удалось запустить выполнение: ресурсы временно исчерпаны.",
            )
    finally:
        _SLOTS.release()


def _execute_in_kernel(req: ExecuteRequest) -> ExecuteResult:
    wall = min(req.timeout or _LIMITS.wall_seconds, _LIMITS.wall_seconds)
    files = [f.model_dump() for f in req.files]
    kr = _REGISTRY.execute(req.conversation_id, req.code, wall, files, req.seed_hash)
    return ExecuteResult(
        status=kr.status,
        stdout=kr.stdout,
        stderr=kr.stderr,
        duration_ms=kr.duration_ms,
        truncated=kr.truncated,
        detail=kr.detail,
        reset=kr.reset,
        results=kr.results or [],
        needs_seed=kr.needs_seed,
    )


def _run_execute(req: ExecuteRequest) -> ExecuteResult:
    wall = min(req.timeout or _LIMITS.wall_seconds, _LIMITS.wall_seconds)
    cap = _LIMITS.output_bytes
    base = _WORKSPACE_ROOT if os.path.isdir(_WORKSPACE_ROOT) else None
    start = time.monotonic()
    out_sink: dict = {}
    err_sink: dict = {}
    timed_out = False
    # Drop this stateless run to its own uid (tr-an0g.7): untrusted code must NOT run
    # as the container-root manager. None when the pool is disabled (no drop). Released
    # in finally AFTER the workdir is cleaned, mirroring the kernel invariant.
    uid = _acquire_stateless_uid()
    reaped = False  # set once proc.wait() confirms the child is gone (gates uid release)
    try:
        with tempfile.TemporaryDirectory(dir=base) as workdir:
            if uid is not None:
                kernels.prepare_uid_workdir(workdir, uid)
            iso_env, iso_kw = kernels.isolate_kwargs(uid, workdir)
            # N.B. (tr-ea8q): the stateless runner has NO result fd / SANDBOX_RESULT_FD and
            # frames nothing back — it is untouched by the persistent-kernel nonce protocol.
            # Do not add a nonce here.
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                [sys.executable, "-I", "-m", "sandbox.runner"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=workdir,
                start_new_session=True,  # own process group → group-kill on cleanup
                env={**os.environ, **iso_env},
                **iso_kw,  # drop to `uid` (C-level user=/group=/extra_groups=) when set
            )  # binary pipes: we cap on raw bytes and decode once at assembly
            out_fd, err_fd = proc.stdout.fileno(), proc.stderr.fileno()
            _set_nonblocking(out_fd)
            _set_nonblocking(err_fd)
            cap_event = threading.Event()
            stop_event = threading.Event()
            readers = [
                threading.Thread(
                    target=_drain, args=(out_fd, cap, out_sink, cap_event, stop_event), daemon=True
                ),
                threading.Thread(
                    target=_drain, args=(err_fd, cap, err_sink, cap_event, stop_event), daemon=True
                ),
            ]
            for t in readers:
                t.start()
            # Feed the code and signal EOF so the runner (sys.stdin.read()) proceeds.
            # The runner reads stdin before producing any output, so this never
            # dead-locks against the reader threads.
            try:
                proc.stdin.write(req.code.encode("utf-8"))
                proc.stdin.close()
            except (BrokenPipeError, ValueError, OSError):
                pass

            # Wait for the child to exit, the output cap to be hit, or the wall clock
            # to expire — whichever comes first. We poll instead of communicate() so a
            # flood can never make us buffer the whole stream before we react.
            deadline = start + wall
            while proc.poll() is None:
                if cap_event.is_set():
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(0.01)

            # Always group-kill: on timeout to stop the runaway, on cap to stop the
            # flood, and on normal completion to sweep any forked-and-left children.
            _kill_group(proc, uid)
            # Release the readers even if an escapee outside the killed group still
            # holds a write end open (so they never park forever / hang pipe.close()).
            stop_event.set()
            for t in readers:
                t.join(timeout=_JOIN_TIMEOUT)
            try:
                proc.wait(timeout=_JOIN_TIMEOUT)
                reaped = True  # child confirmed gone → its uid is safe to recycle
            except subprocess.TimeoutExpired:
                pass
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except (ValueError, OSError):
                    pass

        duration_ms = int((time.monotonic() - start) * 1000)
        if timed_out:
            return ExecuteResult(
                status="timeout",
                stdout="",
                stderr="",
                duration_ms=duration_ms,
                truncated=False,
                detail=f"Превышен лимит времени ({wall}с)",
            )
        out, t1 = _assemble(out_sink, cap)
        err, t2 = _assemble(err_sink, cap)
        rc = proc.returncode
        if rc is not None and rc >= 0:
            # The runner exited on its own: 0 → ok, non-zero → the program errored.
            status = "ok" if rc == 0 else "error"
        else:
            # Negative rc → WE killed it (output cap / cleanup), not the program's own
            # failure. Oversized output is a truncation, not an error (spec §9).
            status = "ok"
        return ExecuteResult(
            status=status,
            stdout=out,
            stderr=err,
            duration_ms=duration_ms,
            truncated=t1 or t2,
        )
    finally:
        # Release the uid only if the child was confirmed reaped; otherwise leak it for
        # /proc-gated reclaim (a live process may still own it). The TemporaryDirectory
        # has already removed the workdir at this point (root + DAC_OVERRIDE).
        _release_stateless_uid(uid, reaped)
