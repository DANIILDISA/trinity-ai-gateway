"""Stateless-path uid pool (epic tr-an0g, Phase 5.0, tr-an0g.7).

The conversation_id=None path (_run_execute) also runs untrusted code, so once the
manager is container-root it MUST drop each stateless run to its own uid too —
otherwise that code would execute as root with the manager's capabilities. The
stateless pool is DISJOINT from the kernel pool (a stateless run and a kernel must
never share a uid). These tests pin the pool bookkeeping; the real setuid drop is
covered by verify_isolation.py.
"""
import collections

import pytest

from sandbox import kernels
from sandbox import server


def test_acquire_release_round_trips(monkeypatch):
    monkeypatch.setattr(server, "_STATELESS_UID_POOL", collections.deque([60032, 60033]))
    a = server._acquire_stateless_uid()
    b = server._acquire_stateless_uid()
    assert {a, b} == {60032, 60033}  # concurrent stateless runs get distinct uids
    server._release_stateless_uid(a)
    assert server._acquire_stateless_uid() == a  # released uid is reissued


def test_acquire_is_none_when_pool_disabled(monkeypatch):
    # Pool unset (backend test container / non-Linux dev) → no drop, Phase 1-4 parity.
    monkeypatch.setattr(server, "_STATELESS_UID_POOL", None)
    assert server._acquire_stateless_uid() is None


def test_acquire_fails_closed_when_pool_enabled_but_empty(monkeypatch):
    # Fail CLOSED: never silently run untrusted code as root because the pool drained.
    monkeypatch.setattr(server, "_STATELESS_UID_POOL", collections.deque())
    monkeypatch.setattr(server, "_STATELESS_LEAKED", set())
    with pytest.raises(kernels.KernelPoolExhausted):
        server._acquire_stateless_uid()


def test_release_leaks_uid_when_not_reaped(monkeypatch):
    """Audit (sec): a stateless run whose child was NOT confirmed-reaped must NOT return
    its uid to the pool (a live process may still own it) — it goes to the leaked set."""
    pool = collections.deque()
    leaked = set()
    monkeypatch.setattr(server, "_STATELESS_UID_POOL", pool)
    monkeypatch.setattr(server, "_STATELESS_LEAKED", leaked)
    server._release_stateless_uid(60032, reaped=True)
    assert list(pool) == [60032] and leaked == set()
    server._release_stateless_uid(60033, reaped=False)
    assert 60033 in leaked and 60033 not in pool


def test_acquire_eager_reclaims_leaked_when_pool_empty(monkeypatch):
    """Audit (low): when the pool is empty, _acquire reclaims a free leaked uid (no live
    holder) and returns it rather than failing."""
    monkeypatch.setattr(server, "_STATELESS_UID_POOL", collections.deque())
    monkeypatch.setattr(server, "_STATELESS_LEAKED", {60035})
    monkeypatch.setattr(kernels, "_live_ruids", lambda: set())  # 60035 has no live holder
    assert server._acquire_stateless_uid() == 60035


def test_pools_must_be_disjoint(monkeypatch):
    """Audit (sec): kernel and stateless uid ranges sharing a uid would let a kernel and
    a stateless run collide — the service must refuse to start on overlap."""
    server._check_pools_disjoint(collections.deque([60000, 60001]), collections.deque([60002]))
    server._check_pools_disjoint(None, collections.deque([60002]))  # unset → no-op
    with pytest.raises(RuntimeError):
        server._check_pools_disjoint(collections.deque([60000, 60001]), collections.deque([60001]))
