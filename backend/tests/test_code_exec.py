"""run_python executor: ExecutionResult→ToolResultPayload mapping + ruble cost (tr-1cp4).

No DB: the executor only talks to the sandbox (mocked here) and prices the run from
config. Mirrors the file-tool contract — never raises on bad input or sandbox failure,
returns ToolResultPayload(status="error", …) instead.
"""
import base64
import math
import types
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.tools.code_exec as code_exec
from app.config import settings
from app.models.conversation import Conversation
from app.models.user import User
from app.services.sandbox.client import ExecutionResult

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def user(db_session: AsyncSession) -> User:
    u = User(
        email="code_exec_test@example.com",
        display_name="Песочница",
        password_hash=None,
        role="user",
        status="active",
        balance_micro_rub=1_000_000,
        reserved_micro_rub=0,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


@pytest_asyncio.fixture
async def conversation(db_session: AsyncSession, user: User) -> Conversation:
    conv = Conversation(user_id=user.id, model_id="anthropic/claude-haiku-4-5", title="A")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    return conv


def _ctx(conversation_id=None, user_id=None):
    return types.SimpleNamespace(
        db=None, user_id=user_id, conversation_id=conversation_id, created_artifact_ids=[]
    )


class _FakeRedis:
    """Async Redis stand-in for the per-user concurrency counter."""

    def __init__(self, n):
        self._n = n  # value incr() reports
        self.incr_calls = 0
        self.decr_calls = 0
        self.expire_calls = 0

    async def incr(self, key):
        self.incr_calls += 1
        return self._n

    async def expire(self, key, ttl):
        self.expire_calls += 1
        return True

    async def decr(self, key):
        self.decr_calls += 1
        return self._n - 1


class _FakeClient:
    """Stands in for SandboxClient: SandboxClient() -> self; execute() -> canned."""

    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.calls = []

    def __call__(self, *a, **k):
        return self

    async def execute(self, *, code, conversation_id=None, timeout=None, files=None,
                      seed_hash=None, http_timeout=None):
        self.calls.append({
            "code": code, "conversation_id": conversation_id, "timeout": timeout,
            "files": files if files is not None else [], "seed_hash": seed_hash,
        })
        if self._exc:
            raise self._exc
        return self._result


class _SeqClient:
    """SandboxClient stand-in returning a SEQUENCE of canned results, one per execute() — for
    the B3 probe→retry protocol (tr-f63e)."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def __call__(self, *a, **k):
        return self

    async def execute(self, *, code, conversation_id=None, timeout=None, files=None,
                      seed_hash=None, http_timeout=None):
        self.calls.append({"files": files or [], "seed_hash": seed_hash, "http_timeout": http_timeout})
        return self._results.pop(0)


def _patch(monkeypatch, result=None, exc=None):
    fake = _FakeClient(result=result, exc=exc)
    monkeypatch.setattr(code_exec, "SandboxClient", fake)
    return fake


def _expected_cost(duration_ms):
    return settings.sandbox_run_base_micro_rub + math.ceil(
        duration_ms * settings.sandbox_sec_micro_rub / 1000
    )


async def test_run_python_ok_maps_stdout_and_cost(monkeypatch):
    _patch(monkeypatch, ExecutionResult(status="ok", stdout="20.5\n", stderr="", duration_ms=120, truncated=False))
    r = await code_exec.run_python({"code": "print(123 / 6)"}, _ctx())
    assert r.status == "ok"
    assert "20.5" in r.output
    assert r.duration_ms == 120
    assert r.truncated is False
    assert r.cost_micro_rub == _expected_cost(120)


async def test_media_type_for_maps_common_extensions():
    """Phase 5 (tr-an0g): generated/seeded files get a correct MIME type by extension,
    with an octet-stream fallback for unknown binaries."""
    assert code_exec.media_type_for("data.csv") == "text/csv"
    assert code_exec.media_type_for("report.json") == "application/json"
    assert code_exec.media_type_for("plot.png") == "image/png"
    assert code_exec.media_type_for("notes.txt") == "text/plain"
    assert code_exec.media_type_for("model.parquet") == "application/octet-stream"
    assert code_exec.media_type_for("noext") == "application/octet-stream"


async def test_media_type_for_blocks_executable_types():
    """Audit B1: executable-class types are never emitted — they download as inert bytes."""
    assert code_exec.media_type_for("report.html") == "application/octet-stream"
    assert code_exec.media_type_for("chart.svg") == "application/octet-stream"
    assert code_exec.media_type_for("payload.js") == "application/octet-stream"
    assert code_exec.media_type_for("page.xhtml") == "application/octet-stream"


async def test_run_python_forwards_rich_results(monkeypatch):
    """Phase 4 (tr-5ktl): the executor carries the sandbox's rich results[] (matplotlib
    PNG, DataFrame HTML) through to the ToolResultPayload for the run-card. An error/
    unreachable path leaves results empty."""
    results = [
        {"type": "image/png", "data": "AAA"},
        {"type": "text/html", "data": "<table></table>"},
    ]
    _patch(monkeypatch, ExecutionResult(
        status="ok", stdout="", stderr="", duration_ms=10, truncated=False, results=results,
    ))
    r = await code_exec.run_python({"code": "df"}, _ctx())
    assert r.status == "ok"
    assert r.results == results


async def test_run_python_persists_png_as_downloadable_artifact(monkeypatch, db_session, user, conversation):
    """Phase 4 (tr-5ktl): a matplotlib PNG result is persisted as an Artifact (reuse the
    artifact system) so it survives reload + downloads. The result dict gains an
    artifact_id + .png filename and the id is linked via created_artifact_ids; the
    artifact row stores the base64 with media_type image/png."""
    from sqlalchemy import select

    from app.models.artifact import Artifact

    png_b64 = base64.b64encode(b"\x89PNG\r\n fake bytes").decode()
    results = [{"type": "image/png", "data": png_b64}]
    _patch(monkeypatch, ExecutionResult(
        status="ok", stdout="", stderr="", duration_ms=5, truncated=False, results=results,
    ))
    ctx = _ctx(conversation_id=conversation.id, user_id=user.id)
    ctx.db = db_session
    r = await code_exec.run_python({"code": "plt.gcf()"}, ctx)
    assert r.status == "ok"
    img = r.results[0]
    assert img["artifact_id"] and img["filename"].endswith(".png")
    assert ctx.created_artifact_ids  # linked so the engine attaches message_id
    art = (
        await db_session.execute(select(Artifact).where(Artifact.filename == img["filename"]))
    ).scalar_one()
    assert art.media_type == "image/png"
    assert art.content == png_b64
    assert art.conversation_id == conversation.id
    # Quota counts the STORED (base64) bytes, matching create_file (audit tr-5ktl).
    assert art.size_bytes == len(png_b64)


async def test_run_python_png_skipped_when_over_user_quota(monkeypatch, db_session, user, conversation):
    """Over the per-user artifact-bytes quota: persistence is skipped — the image stays
    inline-only (live render still works), no artifact row, no created_artifact_ids."""
    from sqlalchemy import func, select

    from app.models.artifact import Artifact
    from app.services.tools import code_exec as ce
    from app.services.tools.file_tools import MAX_ARTIFACT_BYTES_PER_USER

    async def _at_quota(db, uid):
        return MAX_ARTIFACT_BYTES_PER_USER

    monkeypatch.setattr(ce, "_user_total_bytes", _at_quota)
    png_b64 = base64.b64encode(b"\x89PNG over quota").decode()
    _patch(monkeypatch, ExecutionResult(
        status="ok", stdout="", stderr="", duration_ms=5, truncated=False,
        results=[{"type": "image/png", "data": png_b64}],
    ))
    ctx = _ctx(conversation_id=conversation.id, user_id=user.id)
    ctx.db = db_session
    r = await code_exec.run_python({"code": "plt.gcf()"}, ctx)
    assert r.status == "ok"
    assert "artifact_id" not in r.results[0]  # left inline-only
    assert ctx.created_artifact_ids == []
    count = (
        await db_session.execute(select(func.count()).select_from(Artifact))
    ).scalar_one()
    assert count == 0


async def test_run_python_persists_file_as_downloadable_artifact(monkeypatch, db_session, user, conversation):
    """Phase 5 (tr-an0g.3): a generated-file result (the manager already read its bytes
    off the workdir) is persisted as a downloadable Artifact with the extension's MIME
    type, so it survives reload + downloads. The result gains artifact_id; the id is
    linked via created_artifact_ids; the row stores the base64 with size = stored bytes."""
    from sqlalchemy import select

    from app.models.artifact import Artifact

    csv_b64 = base64.b64encode(b"a,b\n1,2\n").decode()
    results = [
        {"type": "file", "filename": "data.csv", "data": csv_b64, "media_type": "text/csv", "size": 8}
    ]
    _patch(monkeypatch, ExecutionResult(
        status="ok", stdout="", stderr="", duration_ms=5, truncated=False, results=results,
    ))
    ctx = _ctx(conversation_id=conversation.id, user_id=user.id)
    ctx.db = db_session
    r = await code_exec.run_python({"code": "open('data.csv','w').write('a,b\\n1,2\\n')"}, ctx)
    assert r.status == "ok"
    f = r.results[0]
    assert f["artifact_id"] and f["filename"] == "data.csv"
    assert ctx.created_artifact_ids
    art = (
        await db_session.execute(select(Artifact).where(Artifact.filename == "data.csv"))
    ).scalar_one()
    assert art.media_type == "text/csv"
    # Text media is stored RAW (artifact convention: create_file + binaryBlob), so the
    # shared download/gallery path reproduces the CSV bytes from the media type alone.
    assert art.content == "a,b\n1,2\n"
    assert art.conversation_id == conversation.id
    assert art.size_bytes == len(b"a,b\n1,2\n")  # stored raw byte length (quota parity)


async def test_run_python_persists_binary_file_as_base64(monkeypatch, db_session, user, conversation):
    """A BINARY generated file (e.g. xlsx) is stored as base64 (like matplotlib PNGs +
    binaryBlob.isBinaryMediaType), so the shared download path base64-decodes it back."""
    from sqlalchemy import select

    from app.models.artifact import Artifact

    raw = b"PK\x03\x04 fake xlsx bytes \x00\x01"
    b64 = base64.b64encode(raw).decode()
    media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    _patch(monkeypatch, ExecutionResult(
        status="ok", stdout="", stderr="", duration_ms=5, truncated=False,
        results=[{"type": "file", "filename": "out.xlsx", "data": b64, "media_type": media, "size": len(raw)}],
    ))
    ctx = _ctx(conversation_id=conversation.id, user_id=user.id)
    ctx.db = db_session
    r = await code_exec.run_python({"code": "wb.save('out.xlsx')"}, ctx)
    assert r.status == "ok"
    art = (
        await db_session.execute(select(Artifact).where(Artifact.filename == "out.xlsx"))
    ).scalar_one()
    assert art.media_type == media
    assert art.content == b64                       # binary → stored base64
    assert base64.b64decode(art.content) == raw
    assert art.size_bytes == len(b64)


async def test_run_python_file_over_quota_stays_inline(monkeypatch, db_session, user, conversation):
    """Over the per-user artifact-bytes quota: persistence is skipped — the file stays
    inline-only (live download from inline base64 still works) and KEEPS its media_type so
    the frontend can still decode it; no artifact row, no created_artifact_ids."""
    from sqlalchemy import func, select

    from app.models.artifact import Artifact
    from app.services.tools import code_exec as ce
    from app.services.tools.file_tools import MAX_ARTIFACT_BYTES_PER_USER

    async def _at_quota(db, uid):
        return MAX_ARTIFACT_BYTES_PER_USER

    monkeypatch.setattr(ce, "_user_total_bytes", _at_quota)
    csv_b64 = base64.b64encode(b"over quota").decode()
    _patch(monkeypatch, ExecutionResult(
        status="ok", stdout="", stderr="", duration_ms=5, truncated=False,
        results=[{"type": "file", "filename": "x.csv", "data": csv_b64, "media_type": "text/csv", "size": 10}],
    ))
    ctx = _ctx(conversation_id=conversation.id, user_id=user.id)
    ctx.db = db_session
    r = await code_exec.run_python({"code": "open('x.csv','w')"}, ctx)
    assert r.status == "ok"
    assert "artifact_id" not in r.results[0]      # inline-only
    assert r.results[0]["media_type"] == "text/csv"  # still decodable by the frontend
    assert ctx.created_artifact_ids == []
    count = (
        await db_session.execute(select(func.count()).select_from(Artifact))
    ).scalar_one()
    assert count == 0


async def test_run_python_seeds_included_files(monkeypatch, db_session, user, conversation):
    """Phase 5 (tr-an0g.4): run_python forwards the conversation's included chat_files to
    the sandbox so the manager seeds workdir/inputs/ before the cell runs."""
    from app.models.chat_file import ChatFile

    db_session.add(ChatFile(
        user_id=user.id, conversation_id=conversation.id, message_id=None,
        filename="data.csv", media_type="text/csv", kind="text", base64_data="YQ==",
        file_size_bytes=1, token_estimate=1, included_in_context=True,
    ))
    await db_session.commit()
    # B3 (tr-f63e): the backend PROBES first (files=[]); on needs_seed it retries with bytes.
    seq = _SeqClient([
        ExecutionResult(status="error", stdout="", stderr="", duration_ms=0, truncated=False, needs_seed=True),
        ExecutionResult(status="ok", stdout="", stderr="", duration_ms=5, truncated=False),
    ])
    monkeypatch.setattr(code_exec, "SandboxClient", seq)
    ctx = _ctx(conversation_id=conversation.id, user_id=user.id)
    ctx.db = db_session
    r = await code_exec.run_python({"code": "import pandas as pd; pd.read_csv('inputs/data.csv')"}, ctx)
    assert r.status == "ok"
    assert len(seq.calls) == 2
    assert seq.calls[0]["files"] == []  # probe carries no bytes
    sent = seq.calls[1]["files"]        # retry carries the upload
    assert [f["filename"] for f in sent] == ["data.csv"]
    assert sent[0]["base64"] == "YQ=="


async def test_run_python_no_files_sends_empty_seed_list(monkeypatch):
    """No db/conversation → no fetch, empty files list, no probe (seed_hash=None)."""
    fake = _patch(monkeypatch, ExecutionResult(
        status="ok", stdout="ok\n", stderr="", duration_ms=5, truncated=False,
    ))
    await code_exec.run_python({"code": "print('ok')"}, _ctx())
    assert len(fake.calls) == 1
    assert fake.calls[0]["files"] == [] and fake.calls[0]["seed_hash"] is None


async def _boom_async(*a, **k):
    raise AssertionError("full-byte fetch must NOT run on a probe-match")


async def test_run_python_no_uploads_single_call_no_probe(monkeypatch, db_session, user, conversation):
    """A conversation with no included files → seed_hash is None → one call, no probe."""
    fake = _patch(monkeypatch, ExecutionResult(status="ok", stdout="", stderr="", duration_ms=5, truncated=False))
    ctx = _ctx(conversation_id=conversation.id, user_id=user.id)
    ctx.db = db_session
    await code_exec.run_python({"code": "1"}, ctx)
    assert len(fake.calls) == 1
    assert fake.calls[0]["seed_hash"] is None and fake.calls[0]["files"] == []


async def test_run_python_probe_match_does_not_fetch_full_bytes(monkeypatch, db_session, user, conversation):
    """Probe-match (manager needs_seed=False) runs the cell WITHOUT fetching/transferring the
    full upload bytes — the whole point of B3."""
    from app.models.chat_file import ChatFile
    db_session.add(ChatFile(
        user_id=user.id, conversation_id=conversation.id, message_id=None,
        filename="d.csv", media_type="text/csv", kind="text", base64_data="YQ==",
        file_size_bytes=1, token_estimate=1, included_in_context=True,
    ))
    await db_session.commit()
    seq = _SeqClient([ExecutionResult(status="ok", stdout="ok", stderr="", duration_ms=7, truncated=False)])
    monkeypatch.setattr(code_exec, "SandboxClient", seq)
    monkeypatch.setattr(code_exec, "included_files_for_sandbox", _boom_async)
    monkeypatch.setattr(code_exec, "included_files_for_sandbox_with_hash", _boom_async)
    ctx = _ctx(conversation_id=conversation.id, user_id=user.id)
    ctx.db = db_session
    r = await code_exec.run_python({"code": "1"}, ctx)
    assert r.status == "ok"
    assert len(seq.calls) == 1
    assert seq.calls[0]["files"] == [] and len(seq.calls[0]["seed_hash"]) == 64


async def test_run_python_needs_seed_retries_with_full_bytes(monkeypatch, db_session, user, conversation):
    """Probe-miss → backend retries with full bytes; only the FINAL run is billed."""
    from app.models.chat_file import ChatFile
    db_session.add(ChatFile(
        user_id=user.id, conversation_id=conversation.id, message_id=None,
        filename="d.csv", media_type="text/csv", kind="text", base64_data="YQ==",
        file_size_bytes=1, token_estimate=1, included_in_context=True,
    ))
    await db_session.commit()
    seq = _SeqClient([
        ExecutionResult(status="error", stdout="", stderr="", duration_ms=0, truncated=False, needs_seed=True),
        ExecutionResult(status="ok", stdout="done", stderr="", duration_ms=42, truncated=False),
    ])
    monkeypatch.setattr(code_exec, "SandboxClient", seq)
    ctx = _ctx(conversation_id=conversation.id, user_id=user.id)
    ctx.db = db_session
    r = await code_exec.run_python({"code": "1"}, ctx)
    assert r.status == "ok" and r.duration_ms == 42
    assert len(seq.calls) == 2
    assert seq.calls[0]["files"] == []
    assert [f["filename"] for f in seq.calls[1]["files"]] == ["d.csv"]
    assert seq.calls[0]["seed_hash"] == seq.calls[1]["seed_hash"]


async def test_probe_uses_no_short_timeout(monkeypatch, db_session, user, conversation):
    """SB-1 (audit pass#2): the probe-match path EXECUTES the cell in the same call, so the
    probe must NOT carry a short http_timeout — a slow (>2s) cell would otherwise spuriously
    time out and trigger a second, duplicate execution."""
    from app.models.chat_file import ChatFile
    db_session.add(ChatFile(
        user_id=user.id, conversation_id=conversation.id, message_id=None,
        filename="d.csv", media_type="text/csv", kind="text", base64_data="YQ==",
        file_size_bytes=1, token_estimate=1, included_in_context=True,
    ))
    await db_session.commit()
    seq = _SeqClient([ExecutionResult(status="ok", stdout="ok", stderr="", duration_ms=9, truncated=False)])
    monkeypatch.setattr(code_exec, "SandboxClient", seq)
    ctx = _ctx(conversation_id=conversation.id, user_id=user.id)
    ctx.db = db_session
    await code_exec.run_python({"code": "1"}, ctx)
    assert len(seq.calls) == 1
    assert seq.calls[0]["http_timeout"] is None


async def test_db_error_on_retry_falls_back_to_none_hash(monkeypatch, db_session, user, conversation):
    """SB-2 (audit pass#2): if fetching the full bytes on a probe-miss raises, the retry must
    send seed_hash=None (kernel clears inputs/ and RUNS the cell) — NOT the original hash,
    which would re-enter the probe branch, miss again, and silently never run the cell."""
    from app.models.chat_file import ChatFile
    db_session.add(ChatFile(
        user_id=user.id, conversation_id=conversation.id, message_id=None,
        filename="d.csv", media_type="text/csv", kind="text", base64_data="YQ==",
        file_size_bytes=1, token_estimate=1, included_in_context=True,
    ))
    await db_session.commit()
    seq = _SeqClient([
        ExecutionResult(status="error", stdout="", stderr="", duration_ms=0, truncated=False, needs_seed=True),
        ExecutionResult(status="ok", stdout="ran", stderr="", duration_ms=11, truncated=False),
    ])
    monkeypatch.setattr(code_exec, "SandboxClient", seq)
    monkeypatch.setattr(code_exec, "included_files_for_sandbox_with_hash", _boom_async)
    ctx = _ctx(conversation_id=conversation.id, user_id=user.id)
    ctx.db = db_session
    r = await code_exec.run_python({"code": "1"}, ctx)
    assert r.status == "ok"  # the cell actually ran
    assert len(seq.calls) == 2
    assert seq.calls[1]["seed_hash"] is None  # retry seeds-empty + runs, no probe re-entry


async def test_run_python_batches_artifact_flush(monkeypatch, db_session, user, conversation):
    """Audit S5: multiple generated artifacts persist in ONE flush round-trip, not one per
    artifact (avoids ~N sequential PG round-trips per run at 1000 concurrent users)."""
    calls = {"n": 0}
    orig_flush = db_session.flush

    async def counting_flush(*a, **k):
        calls["n"] += 1
        return await orig_flush(*a, **k)

    monkeypatch.setattr(db_session, "flush", counting_flush)
    pngs = [
        {"type": "image/png", "data": base64.b64encode(f"img{i}".encode()).decode()} for i in range(3)
    ]
    _patch(monkeypatch, ExecutionResult(
        status="ok", stdout="", stderr="", duration_ms=5, truncated=False, results=pngs,
    ))
    ctx = _ctx(conversation_id=conversation.id, user_id=user.id)
    ctx.db = db_session
    r = await code_exec.run_python({"code": "for i in range(3): plt.figure()"}, ctx)
    assert len([x for x in r.results if x.get("artifact_id")]) == 3
    assert len(ctx.created_artifact_ids) == 3
    assert calls["n"] == 1  # single batched flush, not one per artifact


async def test_run_python_error_surfaces_stderr(monkeypatch):
    _patch(monkeypatch, ExecutionResult(status="error", stdout="", stderr="Traceback (most recent call last):\nValueError: x", duration_ms=80, truncated=False))
    r = await code_exec.run_python({"code": "raise ValueError('x')"}, _ctx())
    assert r.status == "error"
    assert "ValueError" in r.output
    assert r.stderr and "ValueError" in r.stderr
    assert r.cost_micro_rub == _expected_cost(80)


async def test_run_python_timeout_maps_to_error(monkeypatch):
    _patch(monkeypatch, ExecutionResult(status="timeout", stdout="", stderr="", duration_ms=30000, truncated=False, detail="Превышен лимит времени (30с)"))
    r = await code_exec.run_python({"code": "while True:\n    pass"}, _ctx())
    assert r.status == "error"
    assert "лимит времени" in r.output


async def test_run_python_truncated_flag_propagates(monkeypatch):
    _patch(monkeypatch, ExecutionResult(status="ok", stdout="x" * 100, stderr="", duration_ms=50, truncated=True))
    r = await code_exec.run_python({"code": "print('x' * 999999)"}, _ctx())
    assert r.truncated is True


async def test_run_python_sandbox_unreachable_is_graceful_no_charge(monkeypatch):
    fake = _patch(monkeypatch, exc=httpx.ConnectError("refused"))
    r = await code_exec.run_python({"code": "print(1)"}, _ctx())
    assert r.status == "error"
    assert "песочниц" in r.output.lower()
    # Infra failure: the code never ran, so the user is not charged.
    assert r.cost_micro_rub == 0


async def test_run_python_invalid_args_never_hits_sandbox(monkeypatch):
    fake = _patch(monkeypatch, ExecutionResult(status="ok", stdout="", stderr="", duration_ms=1, truncated=False))
    r = await code_exec.run_python({}, _ctx())
    assert r.status == "error"
    assert fake.calls == []


async def test_run_python_cost_is_zero_under_fee_decision_a(monkeypatch):
    # Decision A (spec 2026-06-26 §1): no separate execution fee — cost is 0
    # regardless of duration (tokens already bill the code+output).
    _patch(monkeypatch, ExecutionResult(status="ok", stdout="x", stderr="", duration_ms=2500, truncated=False))
    r = await code_exec.run_python({"code": "print('x')"}, _ctx())
    assert r.cost_micro_rub == 0


async def test_run_python_passes_conversation_id(monkeypatch):
    cid = uuid.uuid4()
    fake = _patch(monkeypatch, ExecutionResult(status="ok", stdout="", stderr="", duration_ms=1, truncated=False))
    await code_exec.run_python({"code": "pass"}, _ctx(conversation_id=cid))
    assert fake.calls[0]["conversation_id"] == str(cid)
    assert fake.calls[0]["timeout"] == settings.sandbox_exec_timeout_seconds


class _FakeRedisExpireFails(_FakeRedis):
    """INCR/DECR work, but EXPIRE raises (a partial-failure Redis stall)."""

    async def expire(self, key, ttl):
        self.expire_calls += 1
        raise RuntimeError("expire timeout")


async def test_run_python_per_user_cap_enforced_even_if_expire_fails(monkeypatch):
    """Audit finding (opt-caps): the cap must be enforced on the known INCR count
    BEFORE the best-effort EXPIRE — an EXPIRE failure must not fail-open past the
    cap, and must roll back the INCR (no counter leak)."""
    red = _FakeRedisExpireFails(n=settings.sandbox_max_concurrent_per_user + 1)
    monkeypatch.setattr(code_exec, "get_redis_client", lambda: red)
    fake = _patch(monkeypatch, ExecutionResult(status="ok", stdout="x", stderr="", duration_ms=1, truncated=False))
    r = await code_exec.run_python({"code": "print(1)"}, _ctx(user_id="u-1"))
    assert r.status == "error"
    assert "одновременных" in r.output.lower()
    assert fake.calls == []        # cap enforced → sandbox never called
    assert red.decr_calls == 1     # INCR rolled back (no leak)


async def test_run_python_slot_released_when_expire_fails_under_cap(monkeypatch):
    """Under cap + EXPIRE fails: the run proceeds and the slot is still DECR'd in
    finally (no leaked counter → no false 'too many runs' lockout later)."""
    red = _FakeRedisExpireFails(n=1)
    monkeypatch.setattr(code_exec, "get_redis_client", lambda: red)
    fake = _patch(monkeypatch, ExecutionResult(status="ok", stdout="ok", stderr="", duration_ms=5, truncated=False))
    r = await code_exec.run_python({"code": "print(1)"}, _ctx(user_id="u-1"))
    assert r.status == "ok"
    assert fake.calls              # ran
    assert red.decr_calls == 1     # slot released in finally (no leak)


async def test_run_python_reset_surfaces_session_note(monkeypatch):
    """tr-gbpc Ф3.6: a kernel reset (timeout/crash/eviction) is surfaced to the
    model so it knows prior variables/imports are gone."""
    import uuid

    _patch(monkeypatch, ExecutionResult(
        status="error", stdout="", stderr="", duration_ms=10, truncated=False,
        detail="Превышен лимит времени (30с)", reset=True,
    ))
    r = await code_exec.run_python(
        {"code": "while True:\n    pass"}, _ctx(conversation_id=uuid.uuid4())
    )
    assert "сброшен" in r.output.lower()


async def test_run_python_503_maps_to_overloaded(monkeypatch):
    """tr-bzk0: the sandbox returns 503 when at global capacity — the executor maps
    it to a distinct 'server overloaded' message the agent can relay."""
    req = httpx.Request("POST", "http://sandbox:9000/execute")
    resp = httpx.Response(503, request=req)
    _patch(monkeypatch, exc=httpx.HTTPStatusError("overloaded", request=req, response=resp))
    r = await code_exec.run_python({"code": "print(1)"}, _ctx())
    assert r.status == "error"
    assert "перегружен" in r.output.lower()


async def test_run_python_per_user_cap_rejects_without_calling_sandbox(monkeypatch):
    """tr-bzk0: over the per-user concurrency cap → immediate reject, the sandbox is
    never called, and the INCR'd slot is released."""
    red = _FakeRedis(n=settings.sandbox_max_concurrent_per_user + 1)
    monkeypatch.setattr(code_exec, "get_redis_client", lambda: red)
    fake = _patch(monkeypatch, ExecutionResult(status="ok", stdout="x", stderr="", duration_ms=1, truncated=False))
    r = await code_exec.run_python({"code": "print(1)"}, _ctx(user_id="u-1"))
    assert r.status == "error"
    assert "одновременных" in r.output.lower()
    assert fake.calls == []
    assert red.decr_calls == 1


async def test_run_python_under_per_user_cap_proceeds_and_releases(monkeypatch):
    red = _FakeRedis(n=1)
    monkeypatch.setattr(code_exec, "get_redis_client", lambda: red)
    fake = _patch(monkeypatch, ExecutionResult(status="ok", stdout="ok", stderr="", duration_ms=5, truncated=False))
    r = await code_exec.run_python({"code": "print(1)"}, _ctx(user_id="u-1"))
    assert r.status == "ok"
    assert fake.calls and fake.calls[0]["code"] == "print(1)"
    assert red.decr_calls == 1  # slot released in finally


async def test_run_python_redis_down_degrades_open(monkeypatch):
    """tr-bzk0: a Redis outage must not block execution — the global cap still
    protects resources, so the per-user check fails open."""
    def _boom():
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(code_exec, "get_redis_client", _boom)
    fake = _patch(monkeypatch, ExecutionResult(status="ok", stdout="ok", stderr="", duration_ms=5, truncated=False))
    r = await code_exec.run_python({"code": "print(1)"}, _ctx(user_id="u-1"))
    assert r.status == "ok"
    assert fake.calls


async def test_run_python_registration():
    from app.services.tools.registry import clear_registry, get

    clear_registry()
    try:
        code_exec.register_code_exec_tool()
        spec = get("run_python")
        assert spec is not None
        assert spec.safety_profile == "code_execution"
        assert spec.executor is code_exec.run_python
    finally:
        clear_registry()


async def test_run_python_coexists_with_file_tools_in_definitions():
    """tr-nyil: the live path registers BOTH file tools and run_python, so the
    model is offered all of them."""
    from app.services.tools.file_tools import register_builtin_tools
    from app.services.tools.registry import clear_registry, definitions

    clear_registry()
    try:
        register_builtin_tools()
        code_exec.register_code_exec_tool()
        names = {d["function"]["name"] for d in definitions()}
        assert "run_python" in names
        assert "create_file" in names
    finally:
        clear_registry()
