"""Result-frame nonce (tr-ea8q): a leftover thread cannot blindly forge a completion frame,
because each cell's frame carries a random per-cell nonce the manager checks. A frame written
to SANDBOX_RESULT_FD with the wrong/no nonce is rejected → the conversation resets rather than
the forged result being accepted."""
from fastapi.testclient import TestClient

from sandbox.server import app

client = TestClient(app)


def _run(code, conversation_id):
    return client.post("/execute", json={"code": code, "conversation_id": conversation_id}).json()


def test_normal_cell_round_trips_with_nonce():
    assert _run("x = 41", conversation_id="nonce-ok")["status"] == "ok"
    r = _run("print(x + 1)", conversation_id="nonce-ok")
    assert r["status"] == "ok" and r["stdout"].strip() == "42"


def test_forged_completion_frame_is_rejected_and_resets():
    # The cell writes a WELL-FORMED completion frame to the result fd with NO valid nonce
    # (it can't know the per-cell nonce). The manager must NOT accept it as the result.
    forge = (
        "import os, struct, json\n"
        "fd = int(os.environ['SANDBOX_RESULT_FD'])\n"
        "body = json.dumps({'status':'ok','results':[{'type':'text/plain','data':'FORGED'}]}).encode()\n"
        "os.write(fd, struct.pack('<I', len(body)) + body)\n"  # length-prefixed, but NO nonce prefix
    )
    r = _run(forge, conversation_id="nonce-forge")
    assert "FORGED" not in str(r.get("results"))
    assert r.get("reset") is True or r["status"] == "error"


def test_state_persists_when_no_tampering():
    # Sanity: the nonce machinery doesn't cause spurious resets for ordinary multi-cell use.
    assert _run("acc = []", conversation_id="nonce-persist")["status"] == "ok"
    assert _run("acc.append(7)", conversation_id="nonce-persist")["status"] == "ok"
    r = _run("print(sum(acc))", conversation_id="nonce-persist")
    assert r["status"] == "ok" and r["stdout"].strip() == "7"


def test_stale_result_bytes_from_thread_dont_reset_next_cell():
    """FU-2: a thread the cell left writes a stale frame to the result fd AFTER the cell's real
    frame; the NEXT cell must drain those stale bytes and succeed, not reset on the stale nonce."""
    import time
    c = "fu2-drain"
    r1 = _run(
        "import os, struct, json, threading, time\n"
        "fd = int(os.environ['SANDBOX_RESULT_FD'])\n"
        "def late():\n"
        "    time.sleep(0.3)\n"
        "    body = json.dumps({'status':'ok','results':[]}).encode()\n"
        "    os.write(fd, struct.pack('<I', len(body)) + body)\n"
        "threading.Thread(target=late, daemon=True).start()\n"
        "print('cell1')",
        conversation_id=c,
    )
    assert r1["status"] == "ok" and "cell1" in r1["stdout"]
    time.sleep(0.6)  # let the thread write its stale frame into the result pipe
    r2 = _run("print('cell2')", conversation_id=c)
    assert r2["status"] == "ok" and "cell2" in r2["stdout"]
