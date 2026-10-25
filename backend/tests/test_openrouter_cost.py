"""fetch_generation_cost — the HTTP client is faked, never hits OpenRouter."""
import pytest

from app.services.openrouter_cost import fetch_generation_cost


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Mimics the subset of httpx.AsyncClient used by fetch_generation_cost."""
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls: list[tuple[str, dict, dict]] = []

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, params or {}, headers or {}))
        return self._response


@pytest.mark.asyncio
async def test_returns_micro_usd_on_success():
    client = _FakeClient(_FakeResponse(200, {"data": {"total_cost": 0.139}}))
    cost = await fetch_generation_cost("sk-or-test", "gen-abc", client=client)
    assert cost == 139000
    url, params, headers = client.calls[0]
    assert params["id"] == "gen-abc"
    assert headers["Authorization"] == "Bearer sk-or-test"


@pytest.mark.asyncio
async def test_returns_none_on_404():
    client = _FakeClient(_FakeResponse(404))
    cost = await fetch_generation_cost("sk-or-test", "gen-missing", client=client)
    assert cost is None


@pytest.mark.asyncio
async def test_returns_none_when_cost_absent():
    client = _FakeClient(_FakeResponse(200, {"data": {}}))
    cost = await fetch_generation_cost("sk-or-test", "gen-abc", client=client)
    assert cost is None
