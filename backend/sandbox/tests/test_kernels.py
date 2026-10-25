"""Persistent-kernel routing via POST /execute (tr-gbpc Ф3.3/Ф3.4).

Same conversation_id → shared state across calls; different ids → isolated;
conversation_id=None → stateless (Phase-1 parity); a per-execution timeout kills
the kernel and resets the conversation (next call starts fresh).
"""
import os
import struct
import threading
import time

import pytest
from fastapi.testclient import TestClient

from sandbox import kernels as kernels_mod
from sandbox.kernels import KernelRegistry
from sandbox.server import app

client = TestClient(app)


def _run(code, conversation_id=None, timeout=None):
    body = {"code": code}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    if timeout is not None:
        body["timeout"] = timeout
    return client.post("/execute", json=body).json()


class TestKernelRouting:
    def test_same_conversation_shares_state(self):
        assert _run("a = 10", conversation_id="k-share")["status"] == "ok"
        r = _run("print(a)", conversation_id="k-share")
        assert r["status"] == "ok"
        assert r["stdout"].strip() == "10"

    def test_other_conversation_is_isolated(self):
        _run("secret = 123", conversation_id="k-iso-1")
        r = _run("print('secret' in dir())", conversation_id="k-iso-2")
        assert r["status"] == "ok"
        assert r["stdout"].strip() == "False"

    def test_none_conversation_is_stateless(self):
        _run("leak = 1", conversation_id=None)
        r = _run("print('leak' in dir())", conversation_id=None)
        assert r["status"] == "ok"
        assert r["stdout"].strip() == "False"

    def test_imports_and_functions_persist(self):
        _run("import math\ndef sq(n):\n    return n * n", conversation_id="k-fn")
        r = _run("print(math.floor(sq(3) + 0.5))", conversation_id="k-fn")
        assert r["status"] == "ok"
        assert r["stdout"].strip() == "9"


class TestKernelTimeoutReset:
    def test_timeout_resets_conversation(self):
        assert _run("x = 1", conversation_id="k-reset")["status"] == "ok"
        r = _run("while True:\n    pass", conversation_id="k-reset", timeout=2)
        assert r["status"] == "timeout"
        # The kernel was killed → the conversation is fresh; x is gone.
        after = _run("print('x' in dir())", conversation_id="k-reset")
        assert after["status"] == "ok"
        assert after["stdout"].strip() == "False"


class TestKernelOutputCap:
    def test_flood_capped_kernel_survives(self):
        r = _run("print('x' * 200000)", conversation_id="k-flood")
        assert r["truncated"] is True
        assert len(r["stdout"]) <= 64 * 1024 + 64
        # Same kernel still alive and stateful afterwards.
        again = _run("print('ok')", conversation_id="k-flood")
        assert again["status"] == "ok"
        assert again["stdout"].strip() == "ok"


class TestKernelLifecycle:
    def _reg(self, **kw):
        kw.setdefault("workdir_base", None)
        kw.setdefault("output_cap", 64 * 1024)
        kw.setdefault("max_kernels", 8)
        return KernelRegistry(**kw)

    def test_idle_kernel_evicted(self):
        reg = self._reg(idle_seconds=0.05)
        try:
            reg.execute("idle", "z = 5", 5)
            assert "idle" in reg._kernels
            time.sleep(0.12)  # exceed the idle window
            assert reg.reap_idle() == 1
            assert "idle" not in reg._kernels
            fresh = reg.execute("idle", "print('z' in dir())", 5)
            assert fresh.stdout.strip() == "False"  # state was reset
        finally:
            reg.shutdown()

    def test_lru_evicts_oldest_at_cap(self):
        reg = self._reg(max_kernels=2)
        try:
            reg.execute("A", "a = 1", 5)
            time.sleep(0.02)
            reg.execute("B", "b = 2", 5)
            time.sleep(0.02)
            reg.execute("C", "c = 3", 5)  # at cap → evict LRU (A)
            assert "A" not in reg._kernels
            assert "B" in reg._kernels and "C" in reg._kernels
            # B kept its state (reusing an existing conv spawns/evicts nothing).
            assert reg.execute("B", "print(b)", 5).stdout.strip() == "2"
            # A comes back fresh.
            assert reg.execute("A", "print('a' in dir())", 5).stdout.strip() == "False"
        finally:
            reg.shutdown()

    def test_spawn_failure_leaks_no_fds_or_workdir(self, monkeypatch, tmp_path):
        """Audit fix (opt-robust): if Popen fails after os.pipe()/mkdtemp (e.g. fork
        hit pids_limit), the pipe fds and the workdir must NOT leak."""
        reg = self._reg(workdir_base=str(tmp_path))

        def _boom(*a, **kw):
            raise OSError("simulated fork failure")

        monkeypatch.setattr(kernels_mod.subprocess, "Popen", _boom)
        fds_before = len(os.listdir("/proc/self/fd"))
        with pytest.raises(OSError):
            reg.execute("c-fail", "x = 1", 5)
        assert list(tmp_path.glob("k-*")) == []  # workdir cleaned
        assert len(os.listdir("/proc/self/fd")) <= fds_before  # pipe fds closed
        reg.shutdown()

    def test_completion_frame_validation(self):
        """Audit fix (sec-protocol): the result-fd frame must carry the cell's nonce + be a
        dict with a valid status — a forged/malformed/wrong-nonce frame is rejected (→ reset),
        never a 500. tr-ea8q: the 8-byte nonce prefix defends against a leftover-thread forge."""
        reg = self._reg()
        try:
            reg.execute("vp", "pass", 5)
            k = reg._kernels["vp"]
            nonce = k._expected_nonce
            assert k._try_parse(bytearray(b"\x01")) is None  # incomplete header

            def _frame(nonce_bytes, body):
                payload = nonce_bytes + body
                return bytearray(struct.pack("<I", len(payload)) + payload)

            ok = b'{"status":"ok","results":[]}'
            assert k._try_parse(_frame(nonce, ok))["status"] == "ok"  # correct nonce → accepted
            with pytest.raises(ValueError):  # wrong nonce (forged by a leftover thread) → reset
                k._try_parse(_frame(b"\x00" * 8, ok))
            with pytest.raises(ValueError):  # no room for the nonce → reset
                k._try_parse(bytearray(struct.pack("<I", 3) + b"abc"))
            for bad in (b'{"foo":1}', b'"hello"', b"123"):  # correct nonce but no status / non-dict
                with pytest.raises(ValueError):
                    k._try_parse(_frame(nonce, bad))
        finally:
            reg.shutdown()

    def test_no_evict_during_execution(self):
        reg = self._reg(idle_seconds=0.0)  # everything idle-eligible…
        results = []

        def run_slow():
            results.append(reg.execute("busy", "import time\ntime.sleep(0.8)\nv = 99", 5))

        t = threading.Thread(target=run_slow)
        t.start()
        try:
            time.sleep(0.3)  # ensure the kernel is mid-execution (lock held)
            assert reg.reap_idle() == 0  # …but a running kernel is never evicted
            assert reg._kernels.get("busy") is not None
        finally:
            t.join()
        assert results[0].status == "ok"
        assert reg.execute("busy", "print(v)", 5).stdout.strip() == "99"
        reg.shutdown()

    def test_rss_eviction_evicts_lru_until_under_threshold(self, monkeypatch):
        """With an RSS threshold set, _get evicts free LRU kernels until the total
        measured RSS fits — even while below the count cap (Phase 4: the DS stack
        makes a single heavy kernel's RSS dominate, so count alone is not enough)."""
        reg = self._reg(max_kernels=4)
        reg._rss_evict_kb = 300_000  # 300 MB budget
        for cid in ("a", "b", "c"):  # three light kernels (~11 MB each, under budget)
            reg.execute(cid, "x = 1", 5)
        # Now pretend every kernel is a 200 MB heavyweight → 3×200 = 600 MB, over budget.
        monkeypatch.setattr(kernels_mod.Kernel, "rss_kb", lambda self: 200_000)
        reg.execute("d", "x = 1", 5)  # spawning a 4th must evict down under the budget
        # Evicts a, then b (400 MB still ≥ 300 MB), stops at 1 existing (200 MB) → +d = 2.
        assert len(reg._kernels) == 2
        reg.shutdown()


class TestUploadSeeding:
    """tr-an0g.4: included upload files in the /execute payload are written into the
    kernel workdir's inputs/ before the cell, and re-seeded (cleaned) every call."""

    def _b64(self, raw: bytes) -> str:
        import base64

        return base64.b64encode(raw).decode("ascii")

    def test_seeded_input_is_readable_by_cell(self):
        files = [{"filename": "data.csv", "base64": self._b64(b"col\n7\n"), "media_type": "text/csv"}]
        r = client.post(
            "/execute",
            json={
                "code": "print(open('inputs/data.csv').read().strip())",
                "conversation_id": "k-upload",
                "files": files,
            },
        ).json()
        assert r["status"] == "ok"
        assert r["stdout"].strip() == "col\n7"

    def test_traversal_filename_seeded_as_basename(self):
        files = [{"filename": "../../evil.csv", "base64": self._b64(b"x")}]
        r = client.post(
            "/execute",
            json={
                "code": "import os; print(os.path.exists('inputs/evil.csv'), os.getcwd())",
                "conversation_id": "k-upload-trav",
                "files": files,
            },
        ).json()
        assert r["status"] == "ok"
        assert r["stdout"].split()[0] == "True"  # landed inside inputs/, basename only

    def test_inputs_recleaned_when_files_absent(self):
        files = [{"filename": "x.csv", "base64": self._b64(b"1")}]
        client.post("/execute", json={"code": "pass", "conversation_id": "k-reseed", "files": files})
        r = client.post(
            "/execute",
            json={
                "code": "import os; print(os.path.exists('inputs/x.csv'))",
                "conversation_id": "k-reseed",
            },
        ).json()
        assert r["status"] == "ok"
        assert r["stdout"].strip() == "False"  # stale input removed on the next call


# --- S4 carry-forward snapshot (tr-ea8q) ---


def _gen_names(resp):
    return [x.get("filename") or x.get("path") for x in (resp.get("results") or [])]


def test_generated_diff_only_reports_current_cell_files():
    """Each cell's download list reflects only files IT created/modified — cell 1's a.txt is
    not re-reported by cell 2 (carry-forward must preserve this two-scan invariant)."""
    c = "ea8q-carry"
    r1 = _run("open('a.txt','w').write('1')", conversation_id=c)
    assert r1["status"] == "ok"
    assert any("a.txt" in str(n) for n in _gen_names(r1))
    r2 = _run("open('b.txt','w').write('2')", conversation_id=c)
    assert r2["status"] == "ok"
    names2 = _gen_names(r2)
    assert any("b.txt" in str(n) for n in names2)
    assert not any("a.txt" in str(n) for n in names2)


def test_leftover_thread_write_not_misattributed_to_next_cell():
    """A thread the cell left running writes a file in the idle window; the thread-gate must
    re-scan the baseline so that file is NOT misattributed to the next cell (audit M1)."""
    c = "ea8q-thread"
    r1 = _run(
        "import threading, time\n"
        "def w():\n"
        "    time.sleep(0.4); open('late.csv','w').write('x')\n"
        "threading.Thread(target=w, daemon=True).start()",
        conversation_id=c,
    )
    assert r1["status"] == "ok"
    time.sleep(0.6)
    r2 = _run("pass", conversation_id=c)
    assert not any("late.csv" in str(n) for n in _gen_names(r2))
