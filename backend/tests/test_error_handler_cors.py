"""Regression test for tr-yxki: the catch-all Exception handler in main.py is
installed by Starlette into ServerErrorMiddleware, which wraps OUTSIDE
CORSMiddleware -- so an unhandled 500 anywhere in the app used to come back
with no Access-Control-Allow-Origin header, and the browser would report an
opaque CORS/ERR_FAILED instead of the intended RU error banner.
"""

from httpx import ASGITransport, AsyncClient

_TEST_PATH = "/__test_unhandled_exception__"


async def _boom() -> None:
    raise RuntimeError("boom - test-only unhandled exception")


async def _get_without_raising(origin: str):
    """A real uvicorn server never propagates an unhandled exception to the
    ASGI caller -- ServerErrorMiddleware always converts it to a response.
    httpx's ASGITransport defaults to re-raising app exceptions instead
    (raise_app_exceptions=True), which would bypass that middleware and give
    a false RED/GREEN signal. raise_app_exceptions=False matches production.
    """
    from app.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        return await client.get(_TEST_PATH, headers={"Origin": origin})


async def test_unhandled_exception_still_returns_cors_headers(
    http_client: AsyncClient,
) -> None:
    """http_client is depended on only to trigger conftest's init_engine/init_redis."""
    from app.main import app

    app.add_api_route(_TEST_PATH, _boom, methods=["GET"])
    try:
        response = await _get_without_raising("http://localhost:3000")
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", None) != _TEST_PATH
        ]

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert response.headers.get("access-control-allow-credentials") == "true"


async def test_unhandled_exception_ignores_unlisted_origin(
    http_client: AsyncClient,
) -> None:
    from app.main import app

    app.add_api_route(_TEST_PATH, _boom, methods=["GET"])
    try:
        response = await _get_without_raising("https://evil.example")
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", None) != _TEST_PATH
        ]

    assert response.status_code == 500
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}
