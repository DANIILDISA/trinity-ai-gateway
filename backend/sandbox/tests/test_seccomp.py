"""Tests for the per-process seccomp filter (epic tr-s3y1 — tr-lsmc, tr-svcz).

The filter is installed INSIDE the runner child (sandbox.runner) right before it
exec's user code, so untrusted code cannot open network sockets (tr-lsmc) or send
signals to the manager / sibling executions (tr-svcz). seccomp is irreversible
per-process, so — exactly like test_runner.py — every test spawns a fresh
`python -I -m sandbox.runner` subprocess and feeds code on stdin; the pytest
process itself is never locked down.
"""
import subprocess
import sys


def _run(code: str) -> subprocess.CompletedProcess:
    """Invoke the runner exactly as the manager will."""
    return subprocess.run(
        [sys.executable, "-I", "-m", "sandbox.runner"],
        input=code,
        capture_output=True,
        text=True,
        timeout=20,
    )


class TestSocketBlocked:
    """tr-lsmc: executed code must not reach ANY network peer (internet OR the
    internal docker network — e.g. backend:8000)."""

    def test_socket_creation_raises_permission_error(self):
        result = _run("import socket; socket.socket()")
        assert result.returncode == 1
        assert "PermissionError" in result.stderr

    def test_internal_peer_unreachable(self):
        """The KNOWN Phase-1 gap: code could socket() to backend over the internal
        network. A numeric IP skips DNS so the failure is the socket() syscall
        itself — the filter denies it before any connect()."""
        code = (
            "import socket\n"
            "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            's.connect(("10.0.0.1", 8000))\n'
        )
        result = _run(code)
        assert result.returncode == 1
        assert "PermissionError" in result.stderr


class TestSignalingBlocked:
    """tr-svcz: signaling is denied unconditionally so code can't os.kill the manager
    (PID 1) or a sibling exec — the primary defence. tr-an0g.7 adds per-conversation
    uids on top, so cross-process signals would also be rejected by the OS uid check."""

    def test_kill_raises_permission_error(self):
        # Signal 0 is the classic "is this pid alive" probe — it sends no signal
        # but still goes through the kill() syscall, so the filter must reject it.
        result = _run("import os; os.kill(os.getpid(), 0)")
        assert result.returncode == 1
        assert "PermissionError" in result.stderr

    def test_kill_manager_pid1_denied(self):
        """The DoS path: os.kill(1, SIGKILL) to take down the manager."""
        result = _run("import os, signal; os.kill(1, signal.SIGKILL)")
        assert result.returncode == 1
        assert "PermissionError" in result.stderr

    def test_rt_tgsigqueueinfo_alternate_path_denied(self):
        """os.kill is blocked, but rt_tgsigqueueinfo is a full signal-sending
        syscall reachable via ctypes (stdlib). With si_code=SI_QUEUE the kernel's
        anti-spoof check passes, so this would deliver SIGKILL to PID 1 unless the
        filter blocks the syscall itself. x86_64 nr=297."""
        code = (
            "import ctypes\n"
            "libc = ctypes.CDLL(None, use_errno=True)\n"
            "buf = ctypes.create_string_buffer(128)\n"
            "rc = libc.syscall(297, 1, 9, buf)\n"  # rt_tgsigqueueinfo(tgid=1, SIGKILL, info)
            "print('rc', rc, 'errno', ctypes.get_errno())\n"
        )
        result = _run(code)
        # The syscall must be denied (EPERM=1); the manager (PID 1) must survive.
        assert result.returncode == 0
        assert "rc -1" in result.stdout
        assert "errno 1" in result.stdout


class TestProcessGroupEscapeBlocked:
    """tr-yd30: untrusted code could os.fork()+os.setsid() (or setpgid) to leave
    the runner's process group, so the manager's os.killpg(runner_pgid) would miss
    the escapee — which keeps the inherited stdout/stderr fds open and DoSes the
    manager. The filter denies setsid/setpgid so a child can never leave the group,
    and killpg always reaps the whole tree."""

    def test_setsid_raises_permission_error(self):
        result = _run("import os; os.setsid()")
        assert result.returncode == 1
        assert "PermissionError" in result.stderr

    def test_setpgid_raises_permission_error(self):
        # os.setpgrp() == setpgid(0, 0): a new process group in the same session —
        # also an escape from killpg(runner_pgid).
        result = _run("import os; os.setpgrp()")
        assert result.returncode == 1
        assert "PermissionError" in result.stderr


class TestBenignSyscallsStillWork:
    """The filter is a denylist — ordinary code must be unaffected."""

    def test_compute_and_print(self):
        result = _run("print(sum(range(10)))")
        assert result.returncode == 0
        assert result.stdout.strip() == "45"

    def test_file_write_in_workspace(self):
        # /tmp is a tmpfs in the sandbox container (the manager runs the runner in
        # a tmpfs workdir); writing there must still work under the filter. A bare
        # relative path would hit the read-only rootfs when run outside the manager.
        result = _run("open('/tmp/scratch.txt', 'w').write('ok'); print('wrote')")
        assert result.returncode == 0
        assert "wrote" in result.stdout
