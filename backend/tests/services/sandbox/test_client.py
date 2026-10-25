"""Tests for the sandbox SandboxClient (epic tr-s3y1, Phase 1)."""
import httpx

from app.services.sandbox.client import SandboxClient, ExecutionResult


def _mock_client(payload: dict) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/execute"
        return httpx.Response(200, json=payload)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_execute_parses_result():
    payload = {"status": "ok", "stdout": "20.5\n", "stderr": "",
               "duration_ms": 12, "truncated": False, "detail": None}
    injected = _mock_client(payload)
    sb = SandboxClient(base_url="http://sandbox:9000", client=injected)
    result = await sb.execute(code="print(123/6)")
    assert isinstance(result, ExecutionResult)
    assert result.status == "ok"
    assert result.stdout.strip() == "20.5"
    # An injected client is owned by the caller — execute() must NOT close it.
    assert injected.is_closed is False


async def test_execute_sends_conversation_id():
    """tr-gbpc Ф3.1: the client must put conversation_id in the request body so the
    manager can route to the conversation's persistent kernel (Phase 3)."""
    import json as _json

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(_json.loads(request.content))
        return httpx.Response(200, json={
            "status": "ok", "stdout": "", "stderr": "",
            "duration_ms": 1, "truncated": False, "detail": None,
        })

    injected = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sb = SandboxClient(base_url="http://sandbox:9000", client=injected)
    await sb.execute(code="x=1", conversation_id="c1")
    assert captured.get("conversation_id") == "c1"


async def test_execute_sends_files():
    """tr-an0g.4: included upload files travel in the request body so the manager can seed
    workdir/inputs/ before the cell. Default (no files) sends an empty list."""
    import json as _json

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(_json.loads(request.content))
        return httpx.Response(200, json={
            "status": "ok", "stdout": "", "stderr": "",
            "duration_ms": 1, "truncated": False, "detail": None,
        })

    injected = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sb = SandboxClient(base_url="http://sandbox:9000", client=injected)
    files = [{"filename": "data.csv", "base64": "YQ==", "media_type": "text/csv"}]
    await sb.execute(code="x=1", conversation_id="c1", files=files)
    assert captured.get("files") == files

    captured.clear()
    await sb.execute(code="x=1")
    assert captured.get("files") == []
