"""Per-UID isolation helpers (epic tr-an0g, Phase 5.0, tr-an0g.7).

Pure-logic tests (no root, no real spawn) for the two small helpers that drive the
per-conversation uid drop: parsing the pool spec from env, and building the Popen
isolation kwargs + per-exec env that confine a child to its own uid and workdir.

The actual setuid/chown drop is exercised end-to-end by verify_isolation.py inside
the hardened sandbox container; here we only pin the logic that decides WHAT to pass.
"""
from sandbox import kernels


class TestParseUidPool:
    def test_range_spec_is_inclusive_deque(self):
        pool = kernels.parse_uid_pool("60000-60003")
        assert list(pool) == [60000, 60001, 60002, 60003]

    def test_unset_returns_none(self):
        assert kernels.parse_uid_pool(None) is None
        assert kernels.parse_uid_pool("") is None

    def test_whitespace_only_returns_none(self):
        assert kernels.parse_uid_pool("   ") is None


class TestIsolateKwargs:
    def test_uid_set_yields_user_group_and_confined_env(self):
        env, kw = kernels.isolate_kwargs(60005, "/workspace/k-abcd")
        # C-level drop (NOT preexec_fn — fork-deadlock hazard in the threaded manager).
        # extra_groups=() is load-bearing: without it the child keeps root's groups.
        assert kw == {"user": 60005, "group": 60005, "extra_groups": ()}
        # tempfile + matplotlib confined INSIDE the 0700 workdir (never shared /tmp),
        # and kept under .tmp so they don't pollute the diff-able workdir root.
        assert env["HOME"] == "/workspace/k-abcd"
        assert env["TMPDIR"] == "/workspace/k-abcd/.tmp"
        assert env["MPLCONFIGDIR"] == "/workspace/k-abcd/.tmp/.mpl"

    def test_uid_none_is_a_noop(self):
        env, kw = kernels.isolate_kwargs(None, "/workspace/k-abcd")
        assert env == {}
        assert kw == {}
