"""Resource limits for sandboxed execution (epic tr-s3y1, Phase 1).

Applied INSIDE the runner subprocess (which runs untrusted code) via setrlimit,
as defense-in-depth on top of the container-level controls in docker-compose.yml
(cpus, mem_limit, pids_limit, no network, read-only rootfs, non-root). The
wall-clock timeout is enforced separately by the manager.
"""
import os
import resource
from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    wall_seconds: int = 30                          # manager-enforced wall clock
    cpu_seconds: int = 30                           # RLIMIT_CPU (hard)
    address_space_bytes: int = 512 * 1024 * 1024    # RLIMIT_AS (~ RAM)
    open_files: int = 64                            # RLIMIT_NOFILE
    file_size_bytes: int = 16 * 1024 * 1024         # RLIMIT_FSIZE
    output_bytes: int = 64 * 1024                   # stdout+stderr cap (manager-side)
    kernel_cpu_seconds: int = 120                   # cumulative CPU backstop (Phase 3 kernel)


def from_env() -> Limits:
    g = os.environ.get
    return Limits(
        wall_seconds=int(g("SANDBOX_WALL_SECONDS", "30")),
        cpu_seconds=int(g("SANDBOX_CPU_SECONDS", "30")),
        address_space_bytes=int(g("SANDBOX_MEM_BYTES", str(512 * 1024 * 1024))),
        open_files=int(g("SANDBOX_NOFILE", "64")),
        file_size_bytes=int(g("SANDBOX_FSIZE_BYTES", str(16 * 1024 * 1024))),
        output_bytes=int(g("SANDBOX_OUTPUT_BYTES", str(64 * 1024))),
        kernel_cpu_seconds=int(g("SANDBOX_KERNEL_CPU_SECONDS", "120")),
    )


def apply(limits: Limits) -> None:
    """Irreversibly lower this process's limits before running untrusted code.
    Fork-bomb protection is the container `pids_limit` (RLIMIT_NPROC is omitted
    because threads count against it and it's brittle)."""
    resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
    resource.setrlimit(
        resource.RLIMIT_AS,
        (limits.address_space_bytes, limits.address_space_bytes),
    )
    resource.setrlimit(resource.RLIMIT_NOFILE, (limits.open_files, limits.open_files))
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (limits.file_size_bytes, limits.file_size_bytes),
    )


def apply_kernel(limits: Limits) -> None:
    """Resident-process limits for a PERSISTENT kernel (Phase 3, tr-gbpc).

    Applies the memory / file-size / open-files ceilings once at kernel startup.
    RLIMIT_CPU is intentionally NOT set here: it caps CUMULATIVE process CPU, which
    over a long-lived kernel would eventually kill a perfectly healthy session.
    Per-execution time is bounded by the manager's wall-clock deadline (which also
    catches sleeping/blocked code); RLIMIT_CPU here is a CUMULATIVE backstop on the
    total CPU one conversation can ever burn (SIGXCPU → kernel dies → reset), so a
    user can't chain many CPU-heavy executions unbounded."""
    resource.setrlimit(
        resource.RLIMIT_CPU,
        (limits.kernel_cpu_seconds, limits.kernel_cpu_seconds),
    )
    resource.setrlimit(
        resource.RLIMIT_AS,
        (limits.address_space_bytes, limits.address_space_bytes),
    )
    resource.setrlimit(resource.RLIMIT_NOFILE, (limits.open_files, limits.open_files))
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (limits.file_size_bytes, limits.file_size_bytes),
    )
