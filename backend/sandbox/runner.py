"""Untrusted-code runner (epic tr-s3y1, Phase 1).

Spawned by the manager as `python -I -m sandbox.runner` (isolated mode). Reads
the user's code from stdin, lowers its own resource limits, then execs the code.
stdout/stderr flow to the real fds (the manager captures them via pipes). Runs
as the container's non-root user in a tmpfs workspace with no network route
(container-level). Exit 0 on success; a traceback to stderr + non-zero exit on
any exception or syntax error.
"""
import sys
import traceback

from sandbox import limits, seccomp


def main() -> int:
    code = sys.stdin.read()
    limits.apply(limits.from_env())

    try:
        compiled = compile(code, "<cell>", "exec")
    except SyntaxError:
        traceback.print_exc()
        return 1

    # Last hardening step before running untrusted code: install the seccomp
    # filter (network + signaling syscalls → EPERM). After this point the runner
    # must not need any blocked syscall itself. Fail-closed: a failure here raises
    # and we never reach exec().
    seccomp.apply()

    namespace: dict = {"__name__": "__main__"}
    try:
        exec(compiled, namespace)  # noqa: S102 - executing user code IS the job
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        return 0 if exc.code is None else 1
    except BaseException:  # noqa: BLE001 - capture everything from user code
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
