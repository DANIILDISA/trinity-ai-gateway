"""Tests for sandbox.server POST /execute and GET /health (epic tr-s3y1, Phase 1).

Strategy: use FastAPI's TestClient (synchronous) so no event-loop gymnastics are
needed. Each POST /execute call spawns a fresh hardened child process, so the
runner's resource limits are exercised without affecting the pytest process.

The timeout test (while True: pass, timeout=2) genuinely waits ~2 s for the
child to be killed — pytest's own timeout budget must accommodate that.
"""
from fastapi.testclient import TestClient

from sandbox.server import app

client = TestClient(app)


class TestHealth:
    def test_health_returns_ok(self):
        """GET /health → {"status": "ok"}."""
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestExecuteSuccess:
    def test_simple_print(self):
        """print(123/6) → status ok, stdout '20.5', empty stderr."""
        resp = client.post("/execute", json={"code": "print(123/6)"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["stdout"].strip() == "20.5"
        assert data["stderr"] == ""
        assert data["duration_ms"] >= 0

    def test_empty_code(self):
        """Empty code must finish cleanly."""
        resp = client.post("/execute", json={"code": ""})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestExecuteError:
    def test_runtime_exception(self):
        """raise ValueError('x') → status error, stderr contains 'ValueError: x'."""
        resp = client.post("/execute", json={"code": "raise ValueError('x')"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "ValueError: x" in data["stderr"]

    def test_syntax_error(self):
        """Malformed code → status error, stderr mentions SyntaxError."""
        resp = client.post("/execute", json={"code": "def broken(\n"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "SyntaxError" in data["stderr"]


class TestExecuteTimeout:
    def test_infinite_loop_times_out(self):
        """while True: pass with timeout=2 → status timeout, detail mentions лимит времени."""
        resp = client.post(
            "/execute",
            json={"code": "while True:\n    pass", "timeout": 2},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "timeout"
        assert data["detail"] is not None
        assert "лимит времени" in data["detail"]


class TestExecuteOutputCap:
    def test_large_output_is_truncated(self):
        """print('x' * 200_000) → truncated=True, len(stdout) <= 64*1024 + 64."""
        resp = client.post("/execute", json={"code": "print('x' * 200000)"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["truncated"] is True
        assert len(data["stdout"]) <= 64 * 1024 + 64

    def test_capped_output_returns_promptly_without_buffering(self):
        """tr-lcmd: a child that floods past the cap and then stays alive must NOT
        force the manager to buffer the whole stream or block until the wall clock.
        The manager caps the read, kills the child, and returns the truncated
        output promptly — not an empty ``timeout`` (which is what a full
        ``communicate()`` does here, while ballooning manager RSS toward OOM)."""
        code = (
            "import sys, time\n"
            "sys.stdout.write('x' * 200000)\n"  # 200 KB ≫ 64 KB cap
            "sys.stdout.flush()\n"
            "time.sleep(30)\n"                   # outlive the 5 s wall clock
        )
        resp = client.post("/execute", json={"code": code, "timeout": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert data["truncated"] is True
        assert data["status"] != "timeout"
        assert len(data["stdout"]) <= 64 * 1024 + 64
        assert data["duration_ms"] < 4000

    def test_stderr_flood_is_capped(self):
        """tr-lcmd: the same bound applies to stderr — a stderr flood is captured
        up to the cap (truncated) without buffering the whole stream."""
        code = (
            "import sys, time\n"
            "sys.stderr.write('y' * 200000)\n"
            "sys.stderr.flush()\n"
            "time.sleep(30)\n"
        )
        resp = client.post("/execute", json={"code": code, "timeout": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert data["truncated"] is True
        assert data["status"] != "timeout"
        assert len(data["stderr"]) <= 64 * 1024 + 64
        assert data["duration_ms"] < 4000

    def test_binary_flood_is_capped(self):
        """tr-lcmd (review Finding D): a non-UTF-8 (binary) flood must still be
        capped and returned promptly — a strict-decode text reader would raise
        UnicodeDecodeError on the first bad byte, lose the cap, and degrade to an
        empty wall-clock timeout. Phase-4 rich output (matplotlib PNG) is binary."""
        code = "import os, time\nos.write(1, b'\\xff' * 200000)\ntime.sleep(30)\n"
        resp = client.post("/execute", json={"code": code, "timeout": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert data["truncated"] is True
        assert data["status"] != "timeout"
        assert data["duration_ms"] < 4000


class TestExecuteConcurrencyCap:
    def test_execute_returns_503_when_at_capacity(self, monkeypatch):
        """tr-bzk0: the manager sheds load immediately (HTTP 503, no queue) when the
        global concurrency cap is saturated, so concurrent runs can't exhaust the
        container's CPU/RAM/pids."""
        import threading as _threading

        from sandbox import server

        full = _threading.BoundedSemaphore(1)
        full.acquire()  # 0 permits left → next acquire(blocking=False) fails
        monkeypatch.setattr(server, "_SLOTS", full)
        resp = client.post("/execute", json={"code": "print(1)"})
        assert resp.status_code == 503

    def test_execute_releases_slot_after_run(self, monkeypatch):
        """A normal run must release its slot so capacity recovers."""
        import threading as _threading

        from sandbox import server

        sem = _threading.BoundedSemaphore(1)
        monkeypatch.setattr(server, "_SLOTS", sem)
        for _ in range(3):  # would fail on the 2nd if the slot leaked
            resp = client.post("/execute", json={"code": "print(1)"})
            assert resp.status_code == 200


class TestExecuteProcessGroupEscape:
    def test_manager_does_not_hang_on_process_group_escape(self, monkeypatch):
        """tr-yd30 (review Finding A), defense-in-depth: even if a child escaped the
        runner's process group and held the inherited stdout open (here we disable
        seccomp to SIMULATE such an escape vector — in production seccomp blocks
        setsid/setpgid), the manager must NOT park forever in the pipe reader or
        block in pipe.close(). It returns promptly with the runner's real output."""
        monkeypatch.setenv("SANDBOX_SECCOMP", "0")
        code = (
            "import os, time, sys\n"
            "if os.fork() == 0:\n"
            "    os.setsid()\n"      # leave the group; keep inherited fd1 open
            "    time.sleep(8)\n"    # bounded so the orphan self-cleans
            "    os._exit(0)\n"
            "sys.stdout.write('done')\n"
            "sys.stdout.flush()\n"
        )
        resp = client.post("/execute", json={"code": code, "timeout": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] != "timeout"
        assert "done" in data["stdout"]
        assert data["duration_ms"] < 4000
