import asyncio
import logging
import secrets
from collections.abc import Callable

from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings
from app.core.redis import get_redis_client

logger = logging.getLogger(__name__)

# 30 MB: 4 images × ~6.7 MB base64 + text/JSON overhead (SPEC body-size limit).
MAX_BODY_SIZE_BYTES = 30 * 1024 * 1024

_MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


# Suffixes of the long-lived SSE chat endpoints, all of which stream a model
# response and routinely outlive request_timeout_seconds (especially with
# extended thinking): the initial send (.../messages) plus the regenerate/edit
# re-generation streams (.../messages/{id}/regenerate, .../messages/{id}/edit).
_SSE_CHAT_SUFFIXES = ("/messages", "/regenerate", "/edit")


def _is_stream_path(path: str) -> bool:
    """True for the long-lived SSE chat endpoints, which must be exempt from the
    request timeout. Non-SSE conversation mutations under /chat/conversations/
    (e.g. /restore, /permanent, /active-path) stay subject to it."""
    return "/chat/conversations/" in path and path.endswith(_SSE_CHAT_SUFFIXES)


_OVERLOADED_MESSAGE = "Сервер перегружен, попробуйте позже"


def overloaded_response(code: str, retry_after: int) -> JSONResponse:
    """Uniform 503 for the backpressure sources (pool-timeout, asyncio-timeout,
    request-timeout), differentiated by `code` and a Retry-After header so clients
    and incident triage can tell them apart instead of seeing three identical
    bodies (tr-wvs). Uses the standard {"error": {...}} envelope."""
    return JSONResponse(
        status_code=503,
        content={"error": {"code": code, "message": _OVERLOADED_MESSAGE, "details": None}},
        headers={"Retry-After": str(retry_after)},
    )


class RequestTimeoutMiddleware:
    """Last-resort backpressure: 503 a request that runs longer than
    settings.request_timeout_seconds, so a single stuck request can't hold a
    worker slot indefinitely under load.

    Pure ASGI on purpose. The BaseHTTPMiddleware version of this regressed the
    single-worker load test (it runs the app in its own task and pumps the
    response over a memory stream — extra per-request service time that, on an
    already-near-saturated worker, tipped the DB pool into timeouts). This wraps
    the downstream call directly with no such overhead.

    The SSE chat stream is exempt (long-lived by design). If the response has
    already started when the deadline hits, we can't replace it, so we re-raise.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _is_stream_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await asyncio.wait_for(
                self.app(scope, receive, send_wrapper),
                timeout=settings.request_timeout_seconds,
            )
        except (asyncio.TimeoutError, TimeoutError):
            if response_started:
                raise
            await overloaded_response("request_timeout", retry_after=5)(
                scope, receive, send
            )


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized request bodies before they're buffered into memory.

    Checks the declared Content-Length; a missing header is allowed through
    (chunked uploads aren't used by this API, and the JSON parser still bounds
    individual fields via the schema's max_length validators).
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_BODY_SIZE_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "error": {
                                "code": "payload_too_large",
                                "message": "Размер запроса превышает 30 МБ",
                                "details": None,
                            }
                        },
                    )
            except ValueError:
                pass
        return await call_next(request)
_EXEMPT_PATHS = frozenset({
    "/api/v1/payments/webhook",
    "/api/v1/payments/webhook/yookassa",
    "/api/v1/health",
})


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Reuse the process-wide Redis client — never construct (or close) one per
        # request. Closing it here would tear down the shared pool mid-flight.
        redis = get_redis_client()
        cookie_token: str = request.cookies.get("csrf_token", "")

        try:
            cookie_in_redis = bool(cookie_token) and bool(
                await redis.exists(f"csrf:{cookie_token}")
            )
            if not cookie_in_redis:
                cookie_token = secrets.token_urlsafe(32)
                ttl = settings.jwt_refresh_token_expire_days * 86400
                await redis.setex(f"csrf:{cookie_token}", ttl, "1")
            redis_ok = True
        except RedisError as exc:
            # tr-34t3 audit#2: never swallow silently — a ResponseError (OOM/ACL) here is a real
            # fault, not just a connection blip. Log so it's visible (behavior unchanged: redis_ok
            # =False → mutating requests 503 below; reads proceed with a locally-minted cookie).
            redis_ok = False
            logger.warning("CSRF Redis check failed (%s: %s) — degraded", type(exc).__name__, exc)
            if not cookie_token:
                cookie_token = secrets.token_urlsafe(32)

        if request.method in _MUTATING_METHODS and request.url.path not in _EXEMPT_PATHS:
            if not redis_ok:
                # tr-34t3 FU-5: standard error envelope (+ Retry-After), consistent with
                # the rest of the Redis-down 503 taxonomy, instead of a bare {"detail"}.
                response = overloaded_response("redis_unavailable", retry_after=2)
                _attach_csrf(response, cookie_token)
                return response

            header_token = request.headers.get("X-CSRF-Token", "")
            try:
                header_valid = bool(header_token) and bool(
                    await redis.exists(f"csrf:{header_token}")
                )
            except RedisError as exc:
                # tr-34t3 FU-5 + audit#2: standard error envelope (+ Retry-After), consistent with
                # the Redis-down 503 taxonomy, and log the fault type so a persistent OOM/ACL
                # isn't misread as a transient blip.
                logger.warning("CSRF Redis header-check failed (%s: %s) — 503", type(exc).__name__, exc)
                response = overloaded_response("redis_unavailable", retry_after=2)
                _attach_csrf(response, cookie_token)
                return response

            if not header_valid:
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "Invalid CSRF token"},
                )
                _attach_csrf(response, cookie_token)
                return response

        response = await call_next(request)
        _attach_csrf(response, cookie_token)
        return response


def _attach_csrf(response: Response, token: str) -> None:
    response.headers["X-CSRF-Token"] = token
    response.set_cookie(
        key="csrf_token",
        value=token,
        httponly=False,
        samesite="strict",
        secure=settings.app_env == "production",
        max_age=settings.jwt_refresh_token_expire_days * 86400,
    )
