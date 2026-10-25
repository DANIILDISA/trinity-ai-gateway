"""B3 two-phase seed-hash (tr-f63e): the kernel re-seeds inputs/ only when the seed_hash
differs from what it last seeded. A probe (files=[] + seed_hash) runs the cell on a match and
answers needs_seed on a miss; a fresh/evicted kernel (seeded_hash=None) always misses. Hashes
are 64-hex (the manager pattern-validates seed_hash, audit N1)."""
import base64

from fastapi.testclient import TestClient

from sandbox.server import app

client = TestClient(app)
HA = "a" * 64
HB = "b" * 64


def _post(body):
    return client.post("/execute", json=body).json()


def _f():
    return {"filename": "d.csv", "base64": base64.b64encode(b"x,y\n1,2\n").decode(), "media_type": "text/csv"}


def test_probe_match_runs_without_reseeding():
    c = "b3-match"
    r1 = _post({"code": "print(open('inputs/d.csv').read().strip().splitlines()[-1])",
                "conversation_id": c, "files": [_f()], "seed_hash": HA})
    assert r1["status"] == "ok" and "1,2" in r1["stdout"] and r1["needs_seed"] is False
    r2 = _post({"code": "print(open('inputs/d.csv').read().strip().splitlines()[-1])",
                "conversation_id": c, "files": [], "seed_hash": HA})
    assert r2["needs_seed"] is False and r2["status"] == "ok" and "1,2" in r2["stdout"]


def test_probe_miss_returns_needs_seed_without_running():
    r = _post({"code": "print('SHOULD_NOT_RUN')", "conversation_id": "b3-miss",
               "files": [], "seed_hash": HB})
    assert r["needs_seed"] is True and "SHOULD_NOT_RUN" not in r["stdout"]


def test_changed_hash_then_full_seed():
    c = "b3-change"
    _post({"code": "1", "conversation_id": c, "files": [_f()], "seed_hash": HA})
    assert _post({"code": "1", "conversation_id": c, "files": [], "seed_hash": HB})["needs_seed"] is True
    ok = _post({"code": "print(open('inputs/d.csv').read().strip().splitlines()[-1])",
                "conversation_id": c, "files": [_f()], "seed_hash": HB})
    assert ok["status"] == "ok" and "1,2" in ok["stdout"]


def test_seed_hash_none_seeds_and_runs():
    r = _post({"code": "import os; print(os.path.isdir('inputs'))", "conversation_id": "b3-none",
               "files": [], "seed_hash": None})
    assert r["status"] == "ok" and r["needs_seed"] is False
