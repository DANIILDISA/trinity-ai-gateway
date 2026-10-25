"""Tests for sandbox.runner (epic tr-s3y1, Phase 1).

Strategy: spawn `python -I -m sandbox.runner` as a subprocess (the same way the
manager will) and feed code on stdin. The subprocess runs in the container's
/app directory so `sandbox` is importable. resource limits are applied inside
the child — the pytest process is unaffected.
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


class TestRunnerSuccess:
    def test_stdout_and_exit_zero(self):
        """print(123/6) → stdout '20.5', returncode 0."""
        result = _run("print(123/6)")
        assert result.returncode == 0
        assert result.stdout.strip() == "20.5"

    def test_empty_code_exits_zero(self):
        """Blank input must not crash — clean exit 0."""
        result = _run("")
        assert result.returncode == 0


class TestRunnerRuntimeError:
    def test_exception_goes_to_stderr_and_nonzero_exit(self):
        """raise ValueError('boom') → returncode 1, stderr contains the error."""
        result = _run("raise ValueError('boom')")
        assert result.returncode == 1
        assert "ValueError" in result.stderr
        assert "boom" in result.stderr

    def test_stdout_not_captured_on_error(self):
        """Output before the exception still reaches stdout."""
        result = _run("print('before')\nraise RuntimeError('after')")
        assert result.returncode == 1
        assert "before" in result.stdout
        assert "RuntimeError" in result.stderr


class TestRunnerSyntaxError:
    def test_syntax_error_reported_on_stderr_not_crash(self):
        """A syntax error must be caught at compile time and reported on stderr."""
        result = _run("def broken(\n")
        assert result.returncode == 1
        assert "SyntaxError" in result.stderr

    def test_syntax_error_does_not_raise_unhandled(self):
        """The runner must not itself crash with an unhandled traceback."""
        result = _run("x = (1 +")
        assert result.returncode == 1
        # stderr should contain SyntaxError, not an internal runner crash
        assert "SyntaxError" in result.stderr


class TestRunnerSystemExit:
    def test_sys_exit_zero(self):
        """sys.exit(0) inside user code → returncode 0."""
        result = _run("import sys; sys.exit(0)")
        assert result.returncode == 0

    def test_sys_exit_nonzero(self):
        """sys.exit(42) inside user code → returncode 42."""
        result = _run("import sys; sys.exit(42)")
        assert result.returncode == 42

    def test_sys_exit_none(self):
        """sys.exit() (no argument) → returncode 0."""
        result = _run("import sys; sys.exit()")
        assert result.returncode == 0
