"""Uid-pool lifecycle in KernelRegistry (epic tr-an0g, Phase 5.0, tr-an0g.7).

The registry hands each conversation's kernel a DISTINCT uid so one tenant's
workspace files (mode-0700, owned by that uid) are unreadable to another. These
tests pin the pool bookkeeping WITHOUT root: os.chown is monkeypatched to a no-op
and the real Kernel (which would spawn a subprocess and setuid) is replaced by a
fake, so we exercise allocation / release / exhaustion as pure logic.

Invariants under test:
  * concurrent kernels get distinct uids
  * a uid returns to the pool after the kernel closes, and is reissued
  * a spawn failure must NOT leak the popped uid (else the pool slowly drains → DoS)
  * the uid is released EXACTLY once — a stale _drop on an already-evicted kernel
    must not double-append it (double-append → two live kernels share one uid =
    cross-tenant breach: the whole point of the scheme)
  * an exhausted pool surfaces a graceful tool error, never an unhandled 500
"""
import os
import threading
import time
import types

import pytest

from sandbox import kernels as kernels_mod
from sandbox.kernels import KernelRegistry, KernelResult


class _FakeKernel:
    """Stand-in for a real Kernel: no subprocess, no setuid. Records the uid the
    registry allocated and cleans up its (real) workdir on close, returning True
    to signal a fully-reaped kernel."""

    def __init__(self, conversation_id, workdir, output_cap, uid=None):
        self.conversation_id = conversation_id
        self.workdir = workdir
        self.uid = uid
        self.lock = threading.Lock()
        self.last_used = time.monotonic()
        self._closed = False

    def alive(self):
        return not self._closed

    def rss_kb(self):
        return 1000

    def execute(self, code, wall_seconds, files=None, seed_hash=None):
        return (
            KernelResult(status="ok", stdout="", stderr="", truncated=False,
                         duration_ms=0, results=[]),
            False,
        )

    def close(self):
        self._closed = True
        kernels_mod.shutil.rmtree(self.workdir, ignore_errors=True)
        return True


class _LeakyKernel(_FakeKernel):
    """A kernel whose teardown is NOT confirmed-reaped (e.g. wait timed out or rmtree
    failed): close() returns False and leaves the workdir behind, so the registry must
    leak the uid (not reuse it) and reclaim it later once /proc shows no live holder."""

    def close(self):
        self._closed = True
        return False  # leave self.workdir on disk


def _registry(monkeypatch, tmp_path, *, uid_pool, max_kernels=4):
    # No root: chown is a no-op; Kernel is the fake (no subprocess / setuid).
    monkeypatch.setattr(kernels_mod.os, "chown", lambda *a, **k: None)
    monkeypatch.setattr(kernels_mod, "Kernel", _FakeKernel)
    return KernelRegistry(
        workdir_base=str(tmp_path), output_cap=1024,
        max_kernels=max_kernels, uid_pool=uid_pool,
    )


def test_distinct_uids_per_kernel(monkeypatch, tmp_path):
    reg = _registry(monkeypatch, tmp_path, uid_pool=range(60000, 60004))
    try:
        reg.execute("A", "x = 1", 5)
        reg.execute("B", "x = 1", 5)
        uids = {reg._kernels["A"].uid, reg._kernels["B"].uid}
        assert len(uids) == 2  # two live tenants never share a uid
    finally:
        reg.shutdown()


def test_uid_returns_to_pool_and_is_reused(monkeypatch, tmp_path):
    reg = _registry(monkeypatch, tmp_path, uid_pool=range(60000, 60002))
    try:
        reg.execute("A", "x = 1", 5)
        reg.execute("B", "x = 1", 5)  # pool now empty
        a_uid = reg._kernels["A"].uid
        reg._drop("A", reg._kernels["A"])  # frees A's uid back to the pool
        reg.execute("C", "x = 1", 5)
        assert reg._kernels["C"].uid == a_uid  # reissued, not leaked
    finally:
        reg.shutdown()


def test_uid_not_leaked_on_spawn_failure(monkeypatch, tmp_path):
    reg = _registry(monkeypatch, tmp_path, uid_pool=range(60000, 60002))

    def _boom(*a, **kw):
        raise OSError("simulated fork failure")

    monkeypatch.setattr(kernels_mod, "Kernel", _boom)
    with pytest.raises(OSError):
        reg.execute("fail", "x = 1", 5)
    # No process ever ran as the popped uid → it must be back immediately.
    assert len(reg._uid_pool) == 2
    reg.shutdown()


def test_drop_releases_uid_exactly_once(monkeypatch, tmp_path):
    """The blocker: only the caller that REMOVED the dict entry may close+release.
    A stale _drop on a kernel another path already evicted must be a no-op."""
    reg = _registry(monkeypatch, tmp_path, uid_pool=range(60000, 60004))
    try:
        reg.execute("A", "x = 1", 5)
        kernel = reg._kernels["A"]
        # Simulate another thread (e.g. an eviction) having popped + released it:
        reg._kernels.pop("A")
        reg._close_and_release(kernel)
        pool_after_owner_release = list(reg._uid_pool)
        # A stale terminal `if dead: self._drop(...)` must NOT re-release the uid.
        reg._drop("A", kernel)
        assert list(reg._uid_pool) == pool_after_owner_release
    finally:
        reg.shutdown()


def test_pool_exhaustion_returns_graceful_error(monkeypatch, tmp_path):
    # One uid, a high count cap → spawning a 2nd kernel finds the pool empty.
    reg = _registry(monkeypatch, tmp_path, uid_pool=range(60000, 60001), max_kernels=8)
    try:
        reg.execute("A", "x = 1", 5)  # takes the only uid
        r = reg.execute("B", "x = 1", 5)  # pool empty
        assert r.status == "error"
        assert r.reset is False  # allocation failed; B's kernel was not reset
        assert "перегруж" in (r.detail or "").lower()
    finally:
        reg.shutdown()


def test_eviction_then_spawn_failure_still_releases_victims(monkeypatch, tmp_path):
    """Audit (sec-code): if _spawn raises AFTER the eviction loop popped victims, those
    victims (process + workdir + uid) must STILL be closed/released — else they leak
    permanently and the pool slowly drains. The drain must run in a finally."""
    reg = _registry(monkeypatch, tmp_path, uid_pool=range(60000, 60002), max_kernels=1)
    reg.execute("A", "x = 1", 5)  # uid 60000 resident, one spare uid left
    a_uid = reg._kernels["A"].uid

    def _boom(*a, **kw):
        raise OSError("simulated fork failure after eviction")

    monkeypatch.setattr(kernels_mod, "Kernel", _boom)
    with pytest.raises(OSError):
        reg.execute("B", "x = 1", 5)  # at cap → evicts A, then _spawn(B) raises
    assert "A" not in reg._kernels  # A was evicted...
    assert a_uid in reg._uid_pool   # ...and its uid returned, not leaked
    assert len(reg._uid_pool) == 2  # both A's and B's popped uid are back
    reg.shutdown()


def test_leaked_uid_and_workdir_reclaimed(monkeypatch, tmp_path):
    """Audit (sec-threat/opt): a uid released without a confirmed reap is held in
    _leaked_uids (keyed to its surviving workdir) and only reclaimed once /proc shows no
    live holder — at which point the leftover workdir is also removed (no tmpfs leak)."""
    monkeypatch.setattr(kernels_mod.os, "chown", lambda *a, **k: None)
    monkeypatch.setattr(kernels_mod, "Kernel", _LeakyKernel)
    reg = KernelRegistry(workdir_base=str(tmp_path), output_cap=1024,
                         max_kernels=4, uid_pool=range(60000, 60002))
    reg.execute("A", "x = 1", 5)
    k = reg._kernels["A"]
    wd = k.workdir
    assert os.path.isdir(wd)
    reg._drop("A", k)  # close() returns False → uid leaked, workdir left behind
    assert reg._leaked_uids.get(k.uid) == wd and k.uid not in reg._uid_pool
    assert os.path.isdir(wd)  # not cleaned yet
    freed = reg._reclaim_leaked_uids()  # no live proc owns 60000 → reclaim + rmtree
    assert freed == 1
    assert k.uid in reg._uid_pool
    assert not os.path.exists(wd)  # leftover workdir removed
    reg.shutdown()


def test_exhaustion_eager_reclaims_before_failing(monkeypatch, tmp_path):
    """Audit (low): when the pool is empty, _spawn must first reclaim any free leaked
    uid and retry — rather than wait up to a full reaper cycle (30 s) to recover."""
    reg = _registry(monkeypatch, tmp_path, uid_pool=range(60000, 60001), max_kernels=4)
    reg.execute("A", "x = 1", 5)  # takes 60000, pool empty
    reg._kernels.pop("A")  # drop A from the map without returning its uid
    leaked_wd = str(tmp_path / "leaked-k")
    os.makedirs(leaked_wd)
    reg._leaked_uids[60000] = leaked_wd  # 60000 leaked (A long gone, no live proc)
    assert not reg._uid_pool
    reg.execute("B", "x = 1", 5)  # must eager-reclaim 60000 and reuse it, not raise
    assert reg._kernels["B"].uid == 60000
    assert not os.path.exists(leaked_wd)  # reclaim cleaned the leftover workdir
    reg.shutdown()


class TestSafeGroupKill:
    def test_killpg_when_leader_alive(self, monkeypatch):
        calls = []
        monkeypatch.setattr(kernels_mod.os, "killpg", lambda pid, sig: calls.append(pid))
        proc = types.SimpleNamespace(pid=1234, poll=lambda: None)  # alive
        kernels_mod.kill_process_group(proc, 60000)
        assert calls == [1234]  # leader holds the pgid → group-kill is safe

    def test_reaped_leader_sweeps_by_pgid_and_uid_never_blind_killpg(self, monkeypatch):
        killpg_calls, killed = [], []
        monkeypatch.setattr(kernels_mod.os, "killpg", lambda *a: killpg_calls.append(a))
        monkeypatch.setattr(kernels_mod.os, "listdir", lambda p: ["1234", "5678", "4242", "self"])
        monkeypatch.setattr(kernels_mod.os, "getpgid",
                            lambda pid: 1234 if pid in (1234, 5678) else 9999)
        monkeypatch.setattr(kernels_mod, "_proc_ruid", lambda pid: 60000 if pid == 5678 else 0)
        monkeypatch.setattr(kernels_mod.os, "kill", lambda pid, sig: killed.append(pid))
        proc = types.SimpleNamespace(pid=1234, poll=lambda: -9)  # leader already reaped
        kernels_mod.kill_process_group(proc, 60000)
        assert killpg_calls == []  # recycled pid → never blind-killpg (cross-tenant kill)
        assert killed == [5678]    # only the pid in OUR pgid AND owned by OUR uid
