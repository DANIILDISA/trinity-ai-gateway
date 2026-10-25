import json

from sqlalchemy import text
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.config import settings
from app.core.database import get_engine
from app.core.redis import get_redis_client, get_redis_pool
from app.api.v1.admin import router as admin_router
from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.files import router as files_router
from app.api.v1.images import router as images_router
from app.api.v1.models import router as models_router
from app.api.v1.payments import router as payments_router
from app.api.v1.users import router as users_router

router = APIRouter()
router.include_router(admin_router, prefix="/admin")
router.include_router(auth_router, prefix="/auth")
router.include_router(chat_router, prefix="/chat")
router.include_router(files_router, prefix="/files")
router.include_router(images_router, prefix="/images")
router.include_router(models_router, prefix="/models")
router.include_router(payments_router, prefix="/payments")
router.include_router(users_router, prefix="/users")


_READINESS_CACHE_KEY = "health:ready"
_READINESS_CACHE_TTL_SECONDS = 5


async def _run_readiness_checks() -> tuple[int, dict]:
    """Probe DB + Redis. Returns (http_status, body). Used by /health and /ready."""
    checks: dict[str, str] = {}

    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "error"

    try:
        client = Redis(connection_pool=get_redis_pool())
        await client.ping()
        await client.aclose()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "error"

    all_ok = all(v == "ok" for v in checks.values())
    body = {
        "status": "healthy" if all_ok else "unhealthy",
        "checks": checks,
        "version": settings.app_version,
    }
    return (200 if all_ok else 503), body


@router.get("/health", response_model=None)
async def health() -> JSONResponse:
    """Full readiness check (DB + Redis). Kept uncached for backward compat with
    the frontend status widget, the health-check cron, and the test suite."""
    status_code, body = await _run_readiness_checks()
    return JSONResponse(status_code=status_code, content=body)


@router.get("/health/live", response_model=None)
async def health_live() -> JSONResponse:
    """Liveness: process is up and serving. No DB/Redis I/O, so it never holds a
    connection — the right probe for the CSRF-bootstrap path and load balancers."""
    return JSONResponse(status_code=200, content={"status": "ok"})


@router.get("/health/ready", response_model=None)
async def health_ready() -> JSONResponse:
    """Readiness: DB + Redis reachable, cached in Redis for 5s so a burst of
    probes doesn't acquire a DB connection per request (load-test bottleneck B3)."""
    redis = get_redis_client()
    try:
        cached = await redis.get(_READINESS_CACHE_KEY)
        if cached is not None:
            payload = json.loads(cached)
            return JSONResponse(status_code=payload["status_code"], content=payload["body"])
    except Exception:
        # Cache read failed — fall through to a live check.
        pass

    status_code, body = await _run_readiness_checks()

    # Only cache a healthy result: caching a 503 could mask a fast recovery, and
    # a transient blip shouldn't pin "unhealthy" for the full TTL.
    if status_code == 200:
        try:
            await redis.setex(
                _READINESS_CACHE_KEY,
                _READINESS_CACHE_TTL_SECONDS,
                json.dumps({"status_code": status_code, "body": body}),
            )
        except Exception:
            pass

    return JSONResponse(status_code=status_code, content=body)
