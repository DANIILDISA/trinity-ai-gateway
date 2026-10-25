"""Tests for RequestTimeoutMiddleware (Phase 3 / M3 backpressure)."""
import pytest
from httpx import AsyncClient

from app.config import settings
from app.core.middleware import _is_stream_path

HEALTH_URL = "/api/v1/health"
MODELS_URL = "/api/v1/models"


def test_stream_path_predicate() -> None:
    # All three SSE chat endpoints are exempt from the timeout: the initial
    # send plus the regenerate/edit re-generation streams, which stream a model
    # response and routinely outlive request_timeout_seconds (esp. with thinking).
    assert _is_stream_path(
        "/api/v1/chat/conversations/2b7c.../messages"
    )
    assert _is_stream_path(
        "/api/v1/chat/conversations/2b7c.../messages/9f1e.../edit"
    )
    assert _is_stream_path(
        "/api/v1/chat/conversations/2b7c.../messages/9f1e.../regenerate"
    )
    # Everything else is subject to it.
    assert not _is_stream_path("/api/v1/models")
    assert not _is_stream_path("/api/v1/chat/conversations")
    assert not _is_stream_path("/api/v1/chat/conversations/2b7c...")
    # Non-SSE conversation mutations under /chat/conversations/ stay subject to it.
    assert not _is_stream_path("/api/v1/chat/conversations/2b7c.../restore")
    assert not _is_stream_path("/api/v1/chat/conversations/2b7c.../permanent")
    assert not _is_stream_path("/api/v1/chat/conversations/2b7c.../active-path")
    assert not _is_stream_path("/api/v1/users/me")


async def test_normal_request_passes_under_default_timeout(
    http_client: AsyncClient,
) -> None:
    # Default 30s ceiling — a healthy request is nowhere near it.
    resp = await http_client.get(HEALTH_URL)
    assert resp.status_code == 200


async def test_request_over_timeout_returns_503(
    http_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A 0s budget means no non-trivial request can finish in time → 503.
    monkeypatch.setattr(settings, "request_timeout_seconds", 0)
    resp = await http_client.get(MODELS_URL)
    assert resp.status_code == 503
    # Distinct code + Retry-After so this 503 is distinguishable from the
    # pool-timeout / io-timeout 503s (tr-wvs).
    body = resp.json()
    assert body["error"]["code"] == "request_timeout"
    assert body["error"]["message"] == "Сервер перегружен, попробуйте позже"
    assert resp.headers["Retry-After"] == "5"


async def test_stream_path_exempt_even_when_timeout_zero(
    http_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even with a 0s budget the SSE path is never short-circuited by the timeout.
    # No balance/proxy here, so the pipeline emits an error SSE — but crucially the
    # response is a normal 200 text/event-stream, NOT the timeout's 503.
    monkeypatch.setattr(settings, "request_timeout_seconds", 0)
    # A conversation we don't own → 404 from the handler (still proves the request
    # reached the handler rather than being 503'd by the timeout middleware).
    resp = await http_client.post(
        "/api/v1/chat/conversations/00000000-0000-0000-0000-000000000000/messages",
        json={"content": "x", "model": "openai/gpt-4.1-mini"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code != 503


async def test_edit_path_exempt_even_when_timeout_zero(
    http_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The /edit SSE re-generation stream is exempt too — a 0s budget must not 503 it.
    monkeypatch.setattr(settings, "request_timeout_seconds", 0)
    resp = await http_client.post(
        "/api/v1/chat/conversations/00000000-0000-0000-0000-000000000000"
        "/messages/00000000-0000-0000-0000-000000000000/edit",
        json={"content": "x", "model": "openai/gpt-4.1-mini"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code != 503


async def test_regenerate_path_exempt_even_when_timeout_zero(
    http_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The /regenerate SSE re-generation stream is exempt too.
    monkeypatch.setattr(settings, "request_timeout_seconds", 0)
    resp = await http_client.post(
        "/api/v1/chat/conversations/00000000-0000-0000-0000-000000000000"
        "/messages/00000000-0000-0000-0000-000000000000/regenerate",
        json={"model": "openai/gpt-4.1-mini"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code != 503
