"""Security containment probes for the sandbox service (epic tr-s3y1, Phase 1).

Run from the BACKEND container (it reaches the sandbox over the internal network):

    docker compose exec -T backend python sandbox/verify_isolation.py

Self-contained (httpx only) so it runs regardless of sys.path. Each CONTAINMENT
probe must be contained (resource-limited / blocked / non-root); the script exits
non-zero if any escapes. The seccomp socket/kill blocks (tr-lsmc, tr-svcz) closed
the former Phase-1 gaps (internal-network peers and same-uid signaling), which are
now containment probes rather than documented limitations.
"""
import asyncio
import base64
import os

import httpx

SANDBOX_URL = os.environ.get("SANDBOX_URL", "http://sandbox:9000")

# label, code, per-exec timeout, predicate(result_dict) -> contained?
CONTAINMENT = [
    (
        "internet egress blocked",
        'import socket; socket.create_connection(("1.1.1.1", 53), timeout=4)',
        8,
        lambda r: r["status"] == "error",
    ),
    (
        "infinite loop killed (wall-clock)",
        "while True:\n    pass",
        3,
        lambda r: r["status"] == "timeout",
    ),
    (
        "memory cap (RLIMIT_AS)",
        "x = bytearray(2_000_000_000)",
        None,
        lambda r: r["status"] == "error" and "MemoryError" in r["stderr"],
    ),
    (
        "fork bomb bounded (pids_limit)",
        "import os\nwhile True:\n    os.fork()",
        6,
        lambda r: r["status"] in ("error", "timeout"),
    ),
    (
        "read-only rootfs (fs escape blocked)",
        'open("/etc/passwd_probe", "w").write("x")',
        None,
        lambda r: r["status"] == "error"
        and any(s in r["stderr"] for s in ("Read-only", "Permission", "OSError")),
    ),
    (
        # tr-an0g.7: the stateless path now drops to a uid from the disjoint stateless
        # pool (60032-60039), not the old shared 65532. Assert it's a non-root pool uid.
        "non-root pool uid",
        "import os; print(os.getuid())",
        None,
        lambda r: r["status"] == "ok"
        and r["stdout"].strip().isdigit()
        and 60000 <= int(r["stdout"].strip()) < 60100,
    ),
    # tr-lsmc: the per-process seccomp filter denies socket()/connect(), so code
    # can't reach internal peers (e.g. backend:8000) — not just the internet. A
    # numeric IP skips DNS so the failure is the socket() syscall itself (EPERM).
    (
        "internal peer blocked (seccomp socket-block)",
        "import socket\n"
        "socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((\"10.0.0.1\", 8000))",
        8,
        lambda r: r["status"] == "error" and "PermissionError" in r["stderr"],
    ),
    # tr-svcz: the seccomp filter denies kill/tkill/tgkill unconditionally, so code can't
    # signal the manager (PID 1) — the primary defence. tr-an0g.7: with the uid pool the
    # child also runs as a distinct 60xxx uid, so the OS uid check would reject it too.
    (
        "signaling manager blocked (seccomp kill-block)",
        "import os, signal; os.kill(1, signal.SIGKILL)",
        None,
        lambda r: r["status"] == "error" and "PermissionError" in r["stderr"],
    ),
    # tr-svcz: os.kill is not the only signal path — rt_tgsigqueueinfo (reachable
    # via ctypes, nr=297 on x86_64) would deliver SIGKILL to PID 1. The filter
    # blocks the syscall (EPERM=1); raw syscall() returns -1 instead of raising,
    # so assert on stdout and confirm the manager (PID 1) is still answering.
    (
        "alternate signal path blocked (rt_tgsigqueueinfo)",
        "import ctypes\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "rc = libc.syscall(297, 1, 9, ctypes.create_string_buffer(128))\n"
        "print(rc, ctypes.get_errno())",
        None,
        lambda r: r["status"] == "ok" and r["stdout"].strip() == "-1 1",
    ),
    # tr-lcmd: an unbounded output flood must NOT make the manager buffer the whole
    # stream (which would balloon its RSS toward mem_limit and cgroup-OOM it). The
    # manager caps the read, kills the flood, and returns promptly with truncated
    # output — never an OOM/hang. The child also stays alive (it loops forever), so
    # this also proves we don't wait for the child to finish before returning.
    (
        "output flood bounded (manager not OOM'd)",
        "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)",
        8,
        lambda r: r["truncated"] is True and len(r["stdout"]) <= 64 * 1024 + 64,
    ),
    # tr-yd30: a child that escapes the runner's process group (setsid) would dodge
    # os.killpg and keep the inherited stdout open, hanging the manager. seccomp now
    # denies setsid/setpgid, so the forked child crashes instead of detaching; the
    # runner still prints and exits cleanly and the manager returns promptly. (If
    # this regresses, the request hangs and the probe trips the client timeout.)
    # tr-an0g (Phase 5): mknod/symlink/link are seccomp-blocked so untrusted code
    # cannot create a FIFO/symlink to win a TOCTOU race against the manager's file
    # reader, nor hardlink-escape its per-uid 0700 workdir.
    (
        "device/symlink/hardlink creation blocked (seccomp mknod/symlink/link)",
        "import os\n"
        "r = []\n"
        "try:\n    os.mkfifo('p')\nexcept PermissionError:\n    r.append('fifo')\n"
        "try:\n    os.symlink('x', 'l')\nexcept PermissionError:\n    r.append('sym')\n"
        "open('a', 'w').close()\n"
        "try:\n    os.link('a', 'b')\nexcept PermissionError:\n    r.append('link')\n"
        "print(','.join(r))",
        None,
        lambda r: r["status"] == "ok" and r["stdout"].strip() == "fifo,sym,link",
    ),
    (
        "process-group escape blocked (setsid → no detach, no hang)",
        "import os, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    time.sleep(30)\n"
        "    os._exit(0)\n"
        "print('parent-done')",
        8,
        lambda r: r["status"] == "ok" and "parent-done" in r["stdout"],
    ),
]


async def _run(client: httpx.AsyncClient, code: str, timeout):
    resp = await client.post(f"{SANDBOX_URL}/execute", json={"code": code, "timeout": timeout})
    resp.raise_for_status()
    return resp.json()


# Count live PIDs in the container. After a fork bomb, this MUST return to near
# baseline — proving the manager group-killed the tree and init reaped orphans,
# not just that the single request errored (the reviewer's catch).
_PID_COUNT = "import os; print(len([p for p in os.listdir('/proc') if p.isdigit()]))"
_RECOVERY_MAX_PIDS = 20


async def main() -> None:
    all_contained = True
    async with httpx.AsyncClient(timeout=30.0) as client:
        print("=== CONTAINMENT PROBES (each must be contained) ===")
        for label, code, timeout, contained in CONTAINMENT:
            r = await _run(client, code, timeout)
            good = contained(r)
            all_contained = all_contained and good
            mark = "CONTAINED" if good else "ESCAPED!!"
            print(
                f"[{mark}] {label}: status={r['status']} "
                f"stdout={r['stdout'].strip()[:50]!r} stderr={r['stderr'].strip()[:90]!r}"
            )

        # Recovery check: the fork bomb above must NOT have left the container
        # pid-exhausted. Count live PIDs; they must be back near baseline.
        rec = await _run(client, _PID_COUNT, None)
        pids = int(rec["stdout"].strip() or "9999")
        recovered = rec["status"] == "ok" and pids <= _RECOVERY_MAX_PIDS
        all_contained = all_contained and recovered
        print(
            f"[{'CONTAINED' if recovered else 'ESCAPED!!'}] fork-bomb recovery: "
            f"live pids={pids} (must be <= {_RECOVERY_MAX_PIDS})"
        )

        # Global concurrency cap (tr-bzk0): a burst beyond SANDBOX_MAX_CONCURRENT
        # must get at least one immediate 503 (load shed, no queue) — proving the
        # manager cannot be driven past its slot budget and exhaust the container.
        async def _status(code: str, timeout):
            try:
                r = await client.post(f"{SANDBOX_URL}/execute", json={"code": code, "timeout": timeout})
                return r.status_code
            except Exception as exc:  # noqa: BLE001
                return repr(exc)

        burst = await asyncio.gather(*[_status("import time; time.sleep(2)", 6) for _ in range(8)])
        shed = sum(1 for c in burst if c == 503)
        cap_ok = shed >= 1
        all_contained = all_contained and cap_ok
        print(
            f"[{'CONTAINED' if cap_ok else 'ESCAPED!!'}] global concurrency cap: "
            f"burst of 8 → {shed}×503 (expect >=1)"
        )

        # Persistent per-conversation kernels (Phase 3, tr-gbpc): state persists
        # within a conversation, is isolated across conversations, hardening still
        # holds inside a kernel, and a timeout/fork-bomb resets the conversation.
        async def _conv(code: str, conv: str, timeout=None):
            r = await client.post(
                f"{SANDBOX_URL}/execute",
                json={"code": code, "timeout": timeout, "conversation_id": conv},
            )
            r.raise_for_status()
            return r.json()

        print("\n=== PERSISTENT KERNEL PROBES (Phase 3) ===")
        await _conv("kp = 41", "vk-a")
        r = await _conv("print(kp + 1)", "vk-a")
        persist_ok = r["status"] == "ok" and r["stdout"].strip() == "42"
        r = await _conv("print('kp' in dir())", "vk-b")
        iso_ok = r["status"] == "ok" and r["stdout"].strip() == "False"
        r = await _conv("import socket; socket.socket()", "vk-a")
        sock_ok = r["status"] == "error" and "PermissionError" in r["stderr"]
        await _conv("rk = 9", "vk-c")
        rt = await _conv("while True:\n    pass", "vk-c", timeout=2)
        rr = await _conv("print('rk' in dir())", "vk-c")
        reset_ok = rt["status"] == "timeout" and rt.get("reset") is True and rr["stdout"].strip() == "False"
        fb = await _conv("import os\nwhile True:\n    os.fork()", "vk-d", timeout=6)
        fb_ok = fb["status"] in ("timeout", "error")
        for label, good in [
            ("kernel state persists in conversation", persist_ok),
            ("kernel conversations isolated", iso_ok),
            ("socket() blocked inside kernel", sock_ok),
            ("per-exec timeout resets conversation", reset_ok),
            ("kernel fork-bomb contained", fb_ok),
        ]:
            all_contained = all_contained and good
            print(f"[{'CONTAINED' if good else 'ESCAPED!!'}] {label}")
        # Poll for reaping: right after a fork-bomb the container can be briefly at
        # pids_limit, so allow a few seconds for the killed group to be reaped.
        rec2_ok, pids2 = False, -1
        for _ in range(20):
            rec2 = await _run(client, _PID_COUNT, None)
            if rec2["status"] == "ok":
                pids2 = int(rec2["stdout"].strip() or "9999")
                if pids2 <= _RECOVERY_MAX_PIDS:
                    rec2_ok = True
                    break
            await asyncio.sleep(0.5)
        all_contained = all_contained and rec2_ok
        print(
            f"[{'CONTAINED' if rec2_ok else 'ESCAPED!!'}] kernel fork-bomb PID recovery: "
            f"live pids={pids2} (must be <= {_RECOVERY_MAX_PIDS})"
        )

        # Rich outputs (Phase 4, tr-5ktl): a matplotlib figure is captured as an
        # image/png result and a trailing DataFrame as text/html — both inside the
        # kernel, delivered in the completion frame's results[] (no network, no files).
        print("\n=== RICH OUTPUT PROBES (Phase 4) ===")
        rp = await _conv("import matplotlib.pyplot as plt\nplt.plot([1, 2, 3])\nplt.gcf()", "vk-rich")
        png_ok = rp["status"] == "ok" and any(
            x.get("type") == "image/png" and x.get("data") for x in (rp.get("results") or [])
        )
        rd = await _conv("import pandas as pd\npd.DataFrame({'a': [1, 2]})", "vk-rich")
        df_ok = rd["status"] == "ok" and any(
            x.get("type") == "text/html" for x in (rd.get("results") or [])
        )
        for label, good in [
            ("matplotlib figure captured as image/png", png_ok),
            ("trailing DataFrame captured as text/html", df_ok),
        ]:
            all_contained = all_contained and good
            print(f"[{'CONTAINED' if good else 'ESCAPED!!'}] {label}")

        # Per-conversation FS isolation (Phase 5.0, tr-an0g.7): each conversation's
        # kernel runs as a DISTINCT uid with a mode-0700 workdir under a 0711 /workspace,
        # so one tenant can neither enumerate nor read another's files (cross-tenant PII).
        # These require the uid pool enabled in the container (SANDBOX_UID_POOL); if the
        # kernel reports a legacy/shared uid they are SKIPPED (not failed).
        print("\n=== UID ISOLATION PROBES (Phase 5.0) ===")
        a = await _conv(
            "import os\nopen('secret.txt', 'w').write('PII-A')\nprint(os.getuid(), os.getcwd())",
            "vk-iso-a",
        )
        parts = a["stdout"].split() if a["status"] == "ok" else []
        a_uid, a_cwd = (parts + ["", ""])[0], (parts + ["", ""])[1]
        pool_on = a_uid.isdigit() and 60000 <= int(a_uid) < 60100
        if not pool_on:
            print(f"[SKIP] uid pool not enabled (kernel uid={a_uid!r}) — isolation probes skipped")
        else:
            b = await _conv("import os; print(os.getuid())", "vk-iso-b")
            b_uid = b["stdout"].strip()
            distinct_ok = b_uid.isdigit() and b_uid != a_uid and 60000 <= int(b_uid) < 60100
            # B cannot listdir /workspace (0711 root-owned → traverse-by-name only).
            ls = await _conv(
                "import os\n"
                "try:\n    os.listdir('/workspace')\n    print('LISTED')\n"
                "except PermissionError:\n    print('DENIED')",
                "vk-iso-b",
            )
            list_denied = ls["status"] == "ok" and ls["stdout"].strip() == "DENIED"
            # B cannot read A's PII even given the exact path (A's 0700 workdir, distinct uid).
            rd = await _conv(
                "import os\n"
                f"p = {a_cwd!r} + '/secret.txt'\n"
                "try:\n    print('READ:' + open(p).read())\n"
                "except (PermissionError, FileNotFoundError) as e:\n    print(type(e).__name__)",
                "vk-iso-b",
            )
            read_denied = rd["status"] == "ok" and rd["stdout"].strip() in (
                "PermissionError", "FileNotFoundError",
            )

            async def _uid_run():
                r = await client.post(
                    f"{SANDBOX_URL}/execute",
                    json={"code": "import os, time\ntime.sleep(0.3)\nprint(os.getuid())", "timeout": 6},
                )
                r.raise_for_status()
                return r.json()["stdout"].strip()

            su = await asyncio.gather(_uid_run(), _uid_run())  # two CONCURRENT stateless runs
            stateless_distinct = len(set(su)) == 2 and all(
                u.isdigit() and 60000 <= int(u) < 60100 for u in su
            )
            for label, good in [
                ("kernel conversations get distinct uids", distinct_ok),
                ("kernel cannot listdir /workspace (0711 traverse-not-list)", list_denied),
                ("kernel cannot read a sibling conversation's 0700 workdir", read_denied),
                ("concurrent stateless runs get distinct uids", stateless_distinct),
            ]:
                all_contained = all_contained and good
                print(f"[{'CONTAINED' if good else 'ESCAPED!!'}] {label}")

        # Workspace file download (Phase 5.2 + 5.3, tr-an0g.2/.3): a cell that writes a
        # file gets its BYTES back as {type:'file', filename, data(base64), media_type,
        # size} — the kernel diffs the workdir, the MANAGER reads the bytes off disk. The
        # matplotlib fontcache (MPLCONFIGDIR=workdir/.tmp/.mpl, dot-prefixed) must NEVER
        # appear (real baked-image check for the tr-19qc dot-entry exclusion).
        print("\n=== WORKSPACE FILE DOWNLOAD PROBES (Phase 5.2/5.3) ===")
        wf = await _conv("open('report.csv', 'w').write('a,b\\n1,2\\n')", "vk-files")
        wf_files = [x for x in (wf.get("results") or []) if x.get("type") == "file"]
        write_ok = (
            wf["status"] == "ok"
            and len(wf_files) == 1
            and wf_files[0].get("filename") == "report.csv"
            and wf_files[0].get("media_type") == "text/csv"
            and base64.b64decode(wf_files[0].get("data", "")) == b"a,b\n1,2\n"
        )
        # matplotlib first-use populates the .mpl fontcache; only the explicit save shows.
        mp = await _conv(
            "import matplotlib.pyplot as plt\n"
            "plt.plot([1, 2, 3])\n"
            "plt.savefig('chart.png')\n"
            "plt.close('all')",
            "vk-files",
        )
        mp_files = [x for x in (mp.get("results") or []) if x.get("type") == "file"]
        dotfree_ok = (
            mp["status"] == "ok"
            and [f.get("filename") for f in mp_files] == ["chart.png"]
            and base64.b64decode(mp_files[0].get("data", ""))[:8] == b"\x89PNG\r\n\x1a\n"
        )
        # ADVERSARIAL (tr-an0g.3): untrusted code can FORGE a completion frame by writing
        # to SANDBOX_RESULT_FD, claiming a file path that escapes the workdir. The manager's
        # realpath-under-workdir + O_NOFOLLOW guard must drop it — NO host file leaks.
        forge = await _conv(
            "import os, json, struct\n"
            "fd = int(os.environ['SANDBOX_RESULT_FD'])\n"
            "f = json.dumps({'status': 'ok', 'results': "
            "[{'type': 'file', 'path': '../../../../../../etc/hostname'}]}).encode()\n"
            "os.write(fd, struct.pack('<I', len(f)) + f)\n",
            "vk-forge",
        )
        forge_files = [x for x in (forge.get("results") or []) if x.get("type") == "file"]
        no_leak_ok = not forge_files  # the escaping path was dropped, nothing returned
        for label, good in [
            ("generated file bytes returned (kernel diff + manager read)", write_ok),
            ("matplotlib fontcache (dot-dir) excluded from file results", dotfree_ok),
            ("forged-frame path traversal dropped (no host-file leak)", no_leak_ok),
        ]:
            all_contained = all_contained and good
            print(f"[{'CONTAINED' if good else 'ESCAPED!!'}] {label}")

        # Upload seeding (Phase 5.4, tr-an0g.4): a file in the /execute payload is written
        # into the kernel workdir's inputs/ and is readable by the DROPPED-uid kernel — the
        # manager chowns it to the exec uid (that chown branch only runs with the uid pool
        # enabled, so this is the real end-to-end check). A traversal filename is reduced to
        # a basename and never escapes inputs/.
        print("\n=== UPLOAD SEEDING PROBES (Phase 5.4) ===")
        up = (await client.post(f"{SANDBOX_URL}/execute", json={
            "code": "print(open('inputs/up.csv').read().strip())",
            "conversation_id": "vk-up",
            "files": [{"filename": "up.csv", "base64": base64.b64encode(b"hello-upload").decode(), "media_type": "text/csv"}],
        })).json()
        upload_ok = up["status"] == "ok" and up["stdout"].strip() == "hello-upload"
        trav = (await client.post(f"{SANDBOX_URL}/execute", json={
            "code": "import os\nprint(os.path.exists('inputs/evil.bin'), os.path.exists('inputs/../../evil.bin'))",
            "conversation_id": "vk-up-trav",
            "files": [{"filename": "../../evil.bin", "base64": base64.b64encode(b"x").decode()}],
        })).json()
        # basename-only: lands at inputs/evil.bin (True), never escapes to /workspace (False).
        trav_ok = trav["status"] == "ok" and trav["stdout"].split() == ["True", "False"]
        for label, good in [
            ("uploaded file seeded + readable by dropped-uid kernel", upload_ok),
            ("traversal upload filename reduced to basename (no escape)", trav_ok),
        ]:
            all_contained = all_contained and good
            print(f"[{'CONTAINED' if good else 'ESCAPED!!'}] {label}")

    print("\n" + ("ALL CONTAINMENT PROBES PASSED" if all_contained else "SOME PROBES ESCAPED"))
    raise SystemExit(0 if all_contained else 1)


if __name__ == "__main__":
    asyncio.run(main())
