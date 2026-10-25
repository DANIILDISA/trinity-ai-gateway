"""Per-process seccomp-BPF filter for the runner (epic tr-s3y1 — tr-lsmc, tr-svcz).

Installed INSIDE the runner child right before it exec's untrusted code, as
defense-in-depth on top of the container controls (no-internet network, cap_drop,
read-only rootfs, RLIMIT). It is *per-process* on purpose: a container-wide
seccomp profile would also strip the manager's own sockets (it serves HTTP), so
only the short-lived runner child loses these syscalls.

What it blocks (denylist, default ALLOW → EPERM on a hit):
  • network (tr-lsmc): socket(), connect() — so executed code has ZERO network
    reach, even to internal peers like backend:8000 (only internet egress was
    blocked before, by the internal-only docker network).
  • signaling (tr-svcz): kill/tkill/tgkill/pidfd_send_signal — block all signal
    sending so code can't os.kill(1, …) to DoS the manager (PID 1) or signal a
    sibling user's exec. (Primary defence; with the tr-an0g.7 per-conversation uid
    pool a child also runs as a distinct uid, so the OS would reject it anyway.)
  • process-group escape (tr-yd30): setsid/setpgid — the manager kills a runaway
    via os.killpg on the runner's group; a child that changes session/group would
    escape the kill and keep the inherited stdout/stderr open, hanging the reader.

Pure stdlib (ctypes + the Linux seccomp/BPF ABI) — deliberately NO third-party
dependency, keeping this untrusted-code image minimal. Fail-closed: if the filter
cannot be installed (unsupported arch / kernel), apply() raises and the runner
refuses to run user code. Set SANDBOX_SECCOMP=0 to opt out on a host without
seccomp (e.g. local dev on a non-Linux kernel) — never in the sandbox container.
"""
import ctypes
import os
import platform

# prctl options
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2

# BPF return actions
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000
EPERM = 1

# BPF instruction opcodes (classic BPF, as used by seccomp)
BPF_LD_W_ABS = 0x20  # BPF_LD | BPF_W | BPF_ABS — load a 32-bit word
BPF_JEQ_K = 0x15     # BPF_JMP | BPF_JEQ | BPF_K — jump-if-equal-to-constant
BPF_JSET_K = 0x45    # BPF_JMP | BPF_JSET | BPF_K — jump-if-(A & k)!=0
BPF_RET_K = 0x06     # BPF_RET | BPF_K — return constant

# Offsets into struct seccomp_data { int nr; __u32 arch; … }
_OFF_NR = 0
_OFF_ARCH = 4

# On x86_64 the x32 ABI reports arch == AUDIT_ARCH_X86_64 but ORs this bit into
# the syscall number, so x32 socket() (41 | bit) would miss every JEQ. Route any
# syscall with this bit set to EPERM so x32 can't sidestep the denylist.
_X32_SYSCALL_BIT = 0x40000000

# Per-arch AUDIT_ARCH value + the syscall numbers we block. Keyed by
# platform.machine(). Only Linux ABIs the sandbox actually runs on are listed;
# anything else is fail-closed (apply() raises).
_AUDIT_ARCH_X86_64 = 0xC000003E
_AUDIT_ARCH_AARCH64 = 0xC00000B7

_SYSCALLS = {
    "x86_64": {
        "audit_arch": _AUDIT_ARCH_X86_64,
        "x32_guard": True,
        "socket": 41,
        "connect": 42,
        "kill": 62,
        "tkill": 200,
        "tgkill": 234,
        "rt_sigqueueinfo": 129,
        "rt_tgsigqueueinfo": 297,
        "pidfd_send_signal": 424,
        "io_uring_setup": 425,
        "setpgid": 109,
        "setsid": 112,
        # Phase 5 (tr-an0g): block device/FIFO + symlink + hardlink creation so
        # untrusted code can't (a) win a symlink/FIFO TOCTOU race against the
        # manager's file reader, or (b) hardlink-escape the per-uid 0700 workdir.
        "mknod": 133,
        "mknodat": 259,
        "symlink": 88,
        "symlinkat": 266,
        "link": 86,
        "linkat": 265,
    },
    "aarch64": {
        "audit_arch": _AUDIT_ARCH_AARCH64,
        "x32_guard": False,
        "socket": 198,
        "connect": 203,
        "kill": 129,
        "tkill": 130,
        "tgkill": 131,
        "rt_sigqueueinfo": 138,
        "rt_tgsigqueueinfo": 240,
        "pidfd_send_signal": 424,
        "io_uring_setup": 425,
        "setpgid": 154,
        "setsid": 157,
        # Phase 5 (tr-an0g): aarch64 has only the *at forms (no legacy
        # mknod/symlink/link syscalls); glibc routes through these.
        "mknodat": 33,
        "symlinkat": 36,
        "linkat": 37,
    },
}

# Order is irrelevant to behaviour; kept stable for readable filter dumps.
# Network egress (socket/connect), every signal-sending syscall (kill family +
# the rt_sigqueueinfo pair that os.kill doesn't cover), and io_uring (whose ops
# run on kernel workers outside this filter — block ring creation entirely).
_BLOCKED = (
    "socket",
    "connect",
    "kill",
    "tkill",
    "tgkill",
    "rt_sigqueueinfo",
    "rt_tgsigqueueinfo",
    "pidfd_send_signal",
    "io_uring_setup",
    # process-group escape (tr-yd30): the manager stops a runaway by os.killpg on
    # the runner's group. A forked child that calls setsid() (new session) or
    # setpgid() (new group) would leave that group, dodge the kill, and keep the
    # inherited stdout/stderr fds open — hanging the manager's pipe reader. Deny
    # both so untrusted code can never leave the group killpg targets.
    "setpgid",
    "setsid",
    # Phase 5 (tr-an0g): device/FIFO + symlink + hardlink creation. Blocking these
    # removes the symlink/FIFO TOCTOU race against the manager's per-file reader and
    # any hardlink escape out of the per-uid 0700 workdir. Legacy forms exist only on
    # x86_64; aarch64 carries just the *at variants (skipped per-arch below).
    "mknod",
    "mknodat",
    "symlink",
    "symlinkat",
    "link",
    "linkat",
)


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_uint16),
        ("jt", ctypes.c_uint8),
        ("jf", ctypes.c_uint8),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    # NOT packed: the kernel's struct sock_fprog has natural alignment, so the
    # filter pointer sits at offset 8 (after 2-byte len + 6-byte padding).
    _fields_ = [
        ("len", ctypes.c_uint16),
        ("filter", ctypes.POINTER(_SockFilter)),
    ]


def is_disabled() -> bool:
    return os.environ.get("SANDBOX_SECCOMP", "1") == "0"


def _instructions(arch: dict) -> list[tuple[int, int, int, int]]:
    """Build the (code, jt, jf, k) tuples for the filter program.

    Layout: verify the arch (kill on mismatch — refuse to run under an ABI whose
    syscall numbers we don't know), load the syscall nr, then a chain of
    "match → jump to EPERM" checks (the x32-bit guard plus one per blocked
    syscall); fall through to ALLOW when nothing matched. Each check's jt is
    computed from the final EPERM index so the offsets can't drift as checks are
    added.
    """
    prog: list[tuple[int, int, int, int]] = [
        (BPF_LD_W_ABS, 0, 0, _OFF_ARCH),
        # arch == ours? skip the kill; else fall through and die.
        (BPF_JEQ_K, 1, 0, arch["audit_arch"]),
        (BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS),
        (BPF_LD_W_ABS, 0, 0, _OFF_NR),
    ]
    # Each check is (opcode, k); jt is filled in below to land on the EPERM ret.
    checks: list[tuple[int, int]] = []
    if arch.get("x32_guard"):
        checks.append((BPF_JSET_K, _X32_SYSCALL_BIT))  # (nr & x32-bit) != 0 → EPERM
    # Some syscalls exist on only one ABI (e.g. legacy mknod/symlink/link are
    # x86_64-only; aarch64 has just the *at forms) — skip names absent on this arch.
    checks.extend((BPF_JEQ_K, arch[name]) for name in _BLOCKED if name in arch)

    # The two terminal returns sit after all the checks: ALLOW (fall-through),
    # then EPERM (the jump target). eperm_idx is the index of the last instruction.
    first_check_idx = len(prog)
    eperm_idx = first_check_idx + len(checks) + 1
    for offset, (opcode, k) in enumerate(checks):
        idx = first_check_idx + offset
        prog.append((opcode, eperm_idx - idx - 1, 0, k))
    prog.append((BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW))          # no match → allow
    prog.append((BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | EPERM))  # match → EPERM
    return prog


def _arch_table() -> dict:
    machine = platform.machine()
    arch = _SYSCALLS.get(machine)
    if arch is None:
        raise RuntimeError(f"seccomp: unsupported architecture {machine!r}")
    return arch


def apply() -> None:
    """Irreversibly install the filter on the calling thread before user code runs.

    Fail-closed: raises on an unsupported arch or any prctl failure, so the runner
    aborts rather than executing untrusted code unfiltered. No-op only when the
    operator explicitly sets SANDBOX_SECCOMP=0.
    """
    if is_disabled():
        return

    prog = _instructions(_arch_table())
    filt = (_SockFilter * len(prog))(*[_SockFilter(*ins) for ins in prog])
    fprog = _SockFprog(len(prog), ctypes.cast(filt, ctypes.POINTER(_SockFilter)))

    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.prctl.restype = ctypes.c_int
    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]

    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, None, 0, 0) != 0:
        raise RuntimeError(f"seccomp: PR_SET_NO_NEW_PRIVS failed (errno {ctypes.get_errno()})")
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        raise RuntimeError(f"seccomp: PR_SET_SECCOMP failed (errno {ctypes.get_errno()})")
