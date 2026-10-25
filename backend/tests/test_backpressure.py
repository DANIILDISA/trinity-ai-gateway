"""tr-3hs: config knobs, OverloadedError, and the backpressure dependency."""
import asyncio

import pytest

pytestmark = pytest.mark.asyncio


def test_config_defaults_present():
    # Assert the CODE-LEVEL field defaults, not the effective values: deployments
    # override pool size etc. via env (.env / .env.prod.example), so reading
    # `settings.x` would couple this test to whatever env is loaded. model_fields
    # gives the default independent of any env override.
    from app.config import Settings

    f = Settings.model_fields
    assert f["database_pool_size"].default == 25  # tr-4fy.3: 20 -> 25
    assert f["database_max_overflow"].default == 15  # tr-4fy.3: 10 -> 15
    assert f["database_pool_timeout"].default == 10
    assert f["worker_limit_concurrency"].default == 200
    assert f["auth_backpressure_limit"].default == 40
    assert f["conversation_backpressure_limit"].default == 50


def test_overloaded_error_shape():
    from app.core.exceptions import OverloadedError, TrinityError
    err = OverloadedError(retry_after=3, code="overloaded_auth")
    assert isinstance(err, TrinityError)
    assert err.http_status == 503
    assert err.retry_after == 3
    assert err.code == "overloaded_auth"
    assert OverloadedError().code == "overloaded"


async def test_backpressure_sheds_when_full():
    from app.core.backpressure import backpressure, get_limiter
    from app.core.exceptions import OverloadedError

    dep = backpressure("test_full", 1, retry_after=3)
    sem = get_limiter("test_full", 1)
    await sem.acquire()  # fill the single slot
    try:
        gen = dep()
        with pytest.raises(OverloadedError) as ei:
            await gen.__anext__()  # locked → raises before acquiring
        assert ei.value.http_status == 503
        assert ei.value.retry_after == 3
        assert ei.value.code == "overloaded_test_full"
    finally:
        sem.release()


async def test_backpressure_acquires_and_releases():
    from app.core.backpressure import backpressure, get_limiter

    dep = backpressure("test_ok", 1)
    sem = get_limiter("test_ok", 1)
    gen = dep()
    await gen.__anext__()        # acquires; yields None
    assert sem.locked()
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()    # runs finally → release
    assert not sem.locked()


async def test_backpressure_releases_on_handler_exception():
    from app.core.backpressure import backpressure, get_limiter

    dep = backpressure("test_exc", 1)
    sem = get_limiter("test_exc", 1)
    gen = dep()
    await gen.__anext__()        # acquires
    assert sem.locked()
    with pytest.raises(RuntimeError):
        await gen.athrow(RuntimeError("handler blew up"))  # finally runs → release
    assert not sem.locked()


def test_get_limiter_is_idempotent():
    from app.core.backpressure import get_limiter
    a = get_limiter("test_idem", 5)
    b = get_limiter("test_idem", 99)  # size ignored once created
    assert a is b
    assert a._value == 5


# ── Integration: limiters mounted on real routes (Task 3) ───────────────────
#
# Each test drains the SAME named semaphore object the route depends on
# (get_limiter is a process-global singleton keyed by name), then drives the
# endpoint through the ASGI app and asserts it sheds (or, for SSE, does NOT
# shed). The drain loop always releases in `finally` so it never leaks slots to
# later tests.

REGISTER_URL = "/api/v1/auth/register"
CONV_URL = "/api/v1/chat/conversations"
MODEL_ID = "openai/gpt-4.1-mini"


async def _register(http_client, email: str) -> str:
    """Register a user and return the bearer access token (matches the pattern
    in test_conversations.py / test_chat_active_path.py)."""
    resp = await http_client.post(
        REGISTER_URL,
        json={"email": email, "password": "SecurePass1", "display_name": "Тест"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_auth_endpoint_sheds_when_limiter_full(http_client):
    from app.config import settings
    from app.core.backpressure import get_limiter

    sem = get_limiter("auth", settings.auth_backpressure_limit)
    drained = 0
    try:
        while not sem.locked():
            await sem.acquire()
            drained += 1
        resp = await http_client.post(
            "/api/v1/auth/login", json={"email": "x@y.z", "password": "nope"}
        )
        assert resp.status_code == 503
        assert resp.headers.get("Retry-After") is not None
        assert resp.json()["error"]["code"] == "overloaded_auth"
    finally:
        for _ in range(drained):
            sem.release()


async def test_conversation_endpoint_sheds_when_limiter_full(http_client):
    from app.config import settings
    from app.core.backpressure import get_limiter

    token = await _register(http_client, "bp_conv_shed@example.com")

    sem = get_limiter("conversation", settings.conversation_backpressure_limit)
    drained = 0
    try:
        while not sem.locked():
            await sem.acquire()
            drained += 1
        resp = await http_client.get(CONV_URL, headers=_auth(token))
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "overloaded_conversation"
    finally:
        for _ in range(drained):
            sem.release()


async def test_sse_send_not_blocked_by_conversation_limiter(http_client):
    # Draining the conversation limiter must NOT 503 the SSE send endpoint (it uses
    # only its own _CHAT_STREAM_SEMAPHORE). It should fail for other reasons
    # (e.g. 404 missing conversation), never 503-overloaded_conversation.
    from app.config import settings
    from app.core.backpressure import get_limiter

    token = await _register(http_client, "bp_sse_unblocked@example.com")

    sem = get_limiter("conversation", settings.conversation_backpressure_limit)
    drained = 0
    try:
        while not sem.locked():
            await sem.acquire()
            drained += 1
        resp = await http_client.post(
            "/api/v1/chat/conversations/00000000-0000-0000-0000-000000000000/messages",
            json={"content": "hi", "model_id": MODEL_ID},
            headers=_auth(token),
        )
        assert not (
            resp.status_code == 503
            and resp.json().get("error", {}).get("code") == "overloaded_conversation"
        )
    finally:
        for _ in range(drained):
            sem.release()
