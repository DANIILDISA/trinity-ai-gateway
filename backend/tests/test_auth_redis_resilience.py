"""tr-8dy3: auth Redis calls must FAIL-OPEN (not 500) when Redis is unavailable — the
blocklist check (security.decode_access_token) and the last_active throttle (require_auth).
Under load the shared Redis pool exhausts and raises ConnectionError/MaxConnectionsError; a
Redis blip must not 500 every authenticated request.
"""
import types

import jwt
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.security import create_access_token, decode_access_token

pytestmark = pytest.mark.asyncio


class _FakeRedis:
    def __init__(self, *, exists_raises=False, exists_result=0, set_raises=False):
        self._exists_raises = exists_raises
        self._exists_result = exists_result
        self._set_raises = set_raises

    async def exists(self, key):
        if self._exists_raises:
            raise RedisConnectionError("Too many connections")
        return self._exists_result

    async def set(self, key, value, ex=None, nx=None):
        if self._set_raises:
            raise RedisConnectionError("Too many connections")
        return True


async def test_decode_fails_open_when_redis_unavailable():
    """Redis down → skip the blocklist check, accept the token (no raise, no 500)."""
    tok = create_access_token(subject="u1")
    payload = await decode_access_token(tok, redis=_FakeRedis(exists_raises=True))
    assert payload["sub"] == "u1"


async def test_decode_still_rejects_revoked_when_redis_up():
    """Control: revocation still works when Redis is healthy (fail-open didn't disable it)."""
    tok = create_access_token(subject="u1")
    with pytest.raises(jwt.InvalidTokenError):
        await decode_access_token(tok, redis=_FakeRedis(exists_result=1))


async def test_decode_accepts_when_not_revoked():
    tok = create_access_token(subject="u1")
    payload = await decode_access_token(tok, redis=_FakeRedis(exists_result=0))
    assert payload["sub"] == "u1"


async def test_require_auth_survives_redis_set_failure():
    """require_auth's last_active throttle must fail-open: return the user without raising and
    WITHOUT committing when redis.set raises (best-effort write is skipped)."""
    from app.dependencies import require_auth

    user = types.SimpleNamespace(id="u1", last_active_at=None)
    committed = {"n": 0}

    class _DB:
        async def commit(self):
            committed["n"] += 1

    result = await require_auth(user=user, db=_DB(), redis=_FakeRedis(set_raises=True))
    assert result is user
    assert committed["n"] == 0  # the last_active write was skipped, not attempted+failed


# --- Layer 2: global Redis-unavailable 503 backstop (tr-8dy3) ---


async def test_redis_connection_error_handler_returns_503():
    """Any unguarded redis.ConnectionError in a request degrades to a graceful 503
    (Retry-After + code redis_unavailable), never a 500."""
    import json
    import redis.exceptions as rexc
    from app.main import app

    handler = app.exception_handlers.get(rexc.ConnectionError)
    assert handler is not None, "no exception handler registered for redis ConnectionError"
    req = types.SimpleNamespace(url=types.SimpleNamespace(path="/x"))
    resp = await handler(req, rexc.MaxConnectionsError("Too many connections"))
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "2"
    assert json.loads(resp.body)["error"]["code"] == "redis_unavailable"


# --- Layer 3: configurable Redis pool size (tr-8dy3) ---


def test_redis_max_connections_default_is_500():
    from app.config import Settings

    assert Settings.model_fields["redis_max_connections"].default == 500


async def test_redis_timeout_error_also_returns_503():
    """A redis TimeoutError (subclass of RedisError, NOT ConnectionError, NOT builtin
    TimeoutError) must ALSO degrade to 503 — not fall through to the 500 catch-all."""
    import json
    import redis.exceptions as rexc
    from app.main import app

    handler = app.exception_handlers.get(rexc.TimeoutError)
    assert handler is not None, "no handler registered for redis TimeoutError → would 500"
    req = types.SimpleNamespace(url=types.SimpleNamespace(path="/x"))
    resp = await handler(req, rexc.TimeoutError("timed out"))
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After")


# --- SB-1: fail-open must be scoped to connection/timeout, NOT data/command errors ---


class _FakeRedisResponseError:
    """redis.exists raises a ResponseError (e.g. NoPermissionError from an ACL misconfig,
    or OutOfMemoryError) — a data/command bug, NOT a transient connection issue."""
    async def exists(self, key):
        from redis.exceptions import ResponseError
        raise ResponseError("NOPERM this user has no permissions to run the 'exists' command")


async def test_decode_does_NOT_fail_open_on_response_error():
    """A ResponseError (ACL/OOM/data bug) on the blocklist check must PROPAGATE (loud 500 +
    alert), NOT silently fail-open and accept a possibly-revoked token."""
    from redis.exceptions import ResponseError
    tok = create_access_token(subject="u1")
    with pytest.raises(ResponseError):
        await decode_access_token(tok, redis=_FakeRedisResponseError())


# ── tr-34t3 FU-3: blocklist fail-open skip-rate metric + escalation ──────────
import time as _time  # noqa: E402

from redis.exceptions import ResponseError as _AuthResponseErr  # noqa: E402

from app.core import security as security_mod  # noqa: E402


async def test_blocklist_skip_escalates_over_threshold(caplog):
    security_mod._blocklist_skips.clear()
    security_mod._last_skip_escalation = 0.0
    with caplog.at_level("WARNING", logger="app.core.security"):
        for _ in range(security_mod._SKIP_ALERT_THRESHOLD):
            security_mod._record_blocklist_skip("jti")
        assert not any(r.levelname == "ERROR" for r in caplog.records)  # at threshold: warn only
        security_mod._record_blocklist_skip("jti")                      # crosses threshold
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "blocklist-skip" in errors[0].getMessage().lower()


async def test_blocklist_skip_escalation_is_rate_limited(caplog):
    security_mod._blocklist_skips.clear()
    security_mod._last_skip_escalation = 0.0
    with caplog.at_level("ERROR", logger="app.core.security"):
        for _ in range(security_mod._SKIP_ALERT_THRESHOLD + 6):
            security_mod._record_blocklist_skip("jti")
    # Many skips in one window → at most ONE escalation error.
    assert sum(r.levelname == "ERROR" for r in caplog.records) == 1


# ── tr-34t3 FU-2: best-effort blocklist write on logout/delete ───────────────
async def test_blacklist_access_token_warns_on_connection_error(caplog):
    class _FailSetex:
        async def setex(self, *a, **k):
            raise RedisConnectionError("down")
    with caplog.at_level("WARNING", logger="app.core.security"):
        await security_mod.blacklist_access_token(_FailSetex(), "jti1", int(_time.time()) + 900)
    assert any("blocklist write skipped" in r.getMessage().lower() for r in caplog.records)


async def test_blacklist_access_token_logs_error_on_response_error(caplog):
    # audit#2: the write path is best-effort — the primary logout/delete already committed,
    # so a ResponseError must NOT 500 the request. But it's not silent either: logged at
    # ERROR (revocation degraded) so monitoring surfaces the persistent fault.
    class _FailSetex:
        async def setex(self, *a, **k):
            raise _AuthResponseErr("OOM")
    with caplog.at_level("ERROR", logger="app.core.security"):
        await security_mod.blacklist_access_token(_FailSetex(), "jti1", int(_time.time()) + 900)
    assert any(
        r.levelname == "ERROR" and "revocation degraded" in r.getMessage().lower()
        for r in caplog.records
    )
