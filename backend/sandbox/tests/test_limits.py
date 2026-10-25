"""Tests for sandbox.limits (epic tr-s3y1, Phase 1).

Strategy for apply() test: mock resource.setrlimit so we don't
permanently lower the pytest process's RLIMIT_AS.
"""
import os
import resource
from unittest.mock import call, patch

import pytest

from sandbox.limits import Limits, apply, from_env


# ---------------------------------------------------------------------------
# from_env() — defaults
# ---------------------------------------------------------------------------

class TestFromEnvDefaults:
    def test_defaults_when_no_env_vars(self, monkeypatch):
        """from_env() returns the documented defaults when no SANDBOX_* vars are set."""
        for var in (
            "SANDBOX_WALL_SECONDS",
            "SANDBOX_CPU_SECONDS",
            "SANDBOX_MEM_BYTES",
            "SANDBOX_NOFILE",
            "SANDBOX_FSIZE_BYTES",
            "SANDBOX_OUTPUT_BYTES",
        ):
            monkeypatch.delenv(var, raising=False)

        limits = from_env()

        assert limits.wall_seconds == 30
        assert limits.cpu_seconds == 30
        assert limits.address_space_bytes == 512 * 1024 * 1024
        assert limits.open_files == 64
        assert limits.file_size_bytes == 16 * 1024 * 1024
        assert limits.output_bytes == 64 * 1024

    def test_returns_limits_instance(self, monkeypatch):
        for var in (
            "SANDBOX_WALL_SECONDS",
            "SANDBOX_CPU_SECONDS",
            "SANDBOX_MEM_BYTES",
            "SANDBOX_NOFILE",
            "SANDBOX_FSIZE_BYTES",
            "SANDBOX_OUTPUT_BYTES",
        ):
            monkeypatch.delenv(var, raising=False)

        limits = from_env()
        assert isinstance(limits, Limits)


# ---------------------------------------------------------------------------
# from_env() — env var overrides
# ---------------------------------------------------------------------------

class TestFromEnvOverrides:
    def test_wall_seconds_override(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_WALL_SECONDS", "10")
        monkeypatch.delenv("SANDBOX_CPU_SECONDS", raising=False)
        monkeypatch.delenv("SANDBOX_MEM_BYTES", raising=False)
        monkeypatch.delenv("SANDBOX_NOFILE", raising=False)
        monkeypatch.delenv("SANDBOX_FSIZE_BYTES", raising=False)
        monkeypatch.delenv("SANDBOX_OUTPUT_BYTES", raising=False)

        limits = from_env()
        assert limits.wall_seconds == 10
        # others keep defaults
        assert limits.cpu_seconds == 30

    def test_mem_bytes_override(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_MEM_BYTES", str(256 * 1024 * 1024))
        monkeypatch.delenv("SANDBOX_WALL_SECONDS", raising=False)
        monkeypatch.delenv("SANDBOX_CPU_SECONDS", raising=False)
        monkeypatch.delenv("SANDBOX_NOFILE", raising=False)
        monkeypatch.delenv("SANDBOX_FSIZE_BYTES", raising=False)
        monkeypatch.delenv("SANDBOX_OUTPUT_BYTES", raising=False)

        limits = from_env()
        assert limits.address_space_bytes == 256 * 1024 * 1024

    def test_all_overrides(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_WALL_SECONDS", "5")
        monkeypatch.setenv("SANDBOX_CPU_SECONDS", "4")
        monkeypatch.setenv("SANDBOX_MEM_BYTES", str(128 * 1024 * 1024))
        monkeypatch.setenv("SANDBOX_NOFILE", "32")
        monkeypatch.setenv("SANDBOX_FSIZE_BYTES", str(8 * 1024 * 1024))
        monkeypatch.setenv("SANDBOX_OUTPUT_BYTES", str(32 * 1024))

        limits = from_env()

        assert limits.wall_seconds == 5
        assert limits.cpu_seconds == 4
        assert limits.address_space_bytes == 128 * 1024 * 1024
        assert limits.open_files == 32
        assert limits.file_size_bytes == 8 * 1024 * 1024
        assert limits.output_bytes == 32 * 1024


# ---------------------------------------------------------------------------
# Limits dataclass properties
# ---------------------------------------------------------------------------

class TestLimitsDataclass:
    def test_frozen(self):
        """Limits must be immutable (frozen=True)."""
        limits = Limits()
        with pytest.raises(Exception):
            limits.wall_seconds = 999  # type: ignore[misc]

    def test_default_construction(self):
        limits = Limits()
        assert limits.wall_seconds == 30
        assert limits.cpu_seconds == 30
        assert limits.address_space_bytes == 512 * 1024 * 1024
        assert limits.open_files == 64
        assert limits.file_size_bytes == 16 * 1024 * 1024
        assert limits.output_bytes == 64 * 1024


# ---------------------------------------------------------------------------
# apply() — mocked setrlimit calls
# ---------------------------------------------------------------------------

class TestApply:
    def test_apply_calls_setrlimit_with_correct_args(self):
        """apply() must call resource.setrlimit with the exact (soft, hard) tuples."""
        limits = Limits(
            cpu_seconds=10,
            address_space_bytes=256 * 1024 * 1024,
            open_files=32,
            file_size_bytes=8 * 1024 * 1024,
        )

        with patch("sandbox.limits.resource.setrlimit") as mock_setrlimit:
            apply(limits)

        expected_calls = [
            call(resource.RLIMIT_CPU, (10, 10)),
            call(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024)),
            call(resource.RLIMIT_NOFILE, (32, 32)),
            call(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024)),
        ]
        mock_setrlimit.assert_has_calls(expected_calls, any_order=False)
        assert mock_setrlimit.call_count == 4

    def test_apply_uses_limits_values(self):
        """apply() reads from the Limits object, not hard-coded constants."""
        limits = Limits(
            cpu_seconds=99,
            address_space_bytes=1 * 1024 * 1024,
            open_files=8,
            file_size_bytes=1024,
        )

        with patch("sandbox.limits.resource.setrlimit") as mock_setrlimit:
            apply(limits)

        recorded = {}
        for c in mock_setrlimit.call_args_list:
            rlimit_const, pair = c.args
            recorded[rlimit_const] = pair

        assert recorded[resource.RLIMIT_CPU] == (99, 99)
        assert recorded[resource.RLIMIT_AS] == (1 * 1024 * 1024, 1 * 1024 * 1024)
        assert recorded[resource.RLIMIT_NOFILE] == (8, 8)
        assert recorded[resource.RLIMIT_FSIZE] == (1024, 1024)
