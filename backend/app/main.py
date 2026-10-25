import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as _RedisConnectionError
from redis.exceptions import TimeoutError as _RedisTimeoutError
from sqlalchemy import select, update
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from app.config import settings
from app.core.database import dispose_engine, get_session_factory, init_engine
from app.core.exceptions import TrinityError
from app.core.middleware import (
    BodySizeLimitMiddleware,
    CSRFMiddleware,
    RequestTimeoutMiddleware,
    overloaded_response,
)
from app.core.redis import close_redis, get_redis_pool, init_redis
from app.core.security import encrypt_key
from app.models.api_key import ApiKey
from app.services.model_sync_service import ModelSyncService
from app.tasks.cleanup import (
    reconcile_partial_costs,
    reconcile_reserved_drift,
    release_expired_reservations,
)
from app.tasks.exchange_rate import run_exchange_rate_update
from app.tasks.health_check import check_openrouter_health
from app.tasks.reconciliation import run_daily_reconciliation
from app.api.v1.router import router as api_v1_router

logger = logging.getLogger(__name__)

_MODEL_SYNC_INTERVAL_SECONDS = 24 * 60 * 60
_DAILY_RECONCILIATION_INTERVAL_SECONDS = 24 * 60 * 60
# The updater self-throttles to auto_exchange_rate_interval_minutes internally;
# we just poke it once a minute, the same cadence its docstring assumes for cron.
_EXCHANGE_RATE_TICK_SECONDS = 60
# Every-minute reservation reaper + partial-cost reconcile. These were host-cron
# only (scripts/setup_cron.sh), but that crontab is never installed in the Docker
# deployment, so the reaper never ran and balances stuck on leaked reservations
# (tr-u3y5). Running it in-process under the leader lock makes it actually fire.
_RESERVATION_CLEANUP_TICK_SECONDS = 60

# Background-task leader election (gunicorn runs N worker processes; the periodic
# loops — model sync, health probe, exchange-rate poll — must run in exactly ONE
# of them, not N). The winner of this Redis lock is the leader; others skip all
# background work. TTL 60s, renewed every 30s so the lock survives a slow tick
# but is released within a minute if the leader dies.
_LEADER_LOCK_KEY = "trinity:bg-leader"
_LEADER_LOCK_TTL_SECONDS = 60
_LEADER_RENEW_INTERVAL_SECONDS = 30
# tr-2ubv.24: followers retry the acquire on this cadence so the fleet
# self-heals once the stale lock expires / Redis recovers. TTL/2 → a dead
# leader's lock is re-taken within ~90s worst-case.
_FOLLOWER_RETRY_INTERVAL_SECONDS = 30
_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def _is_real_openrouter_key(value: str) -> bool:
    """True only for a genuine OpenRouter key, not a dev placeholder.

    Real keys are prefixed `sk-or-`. Placeholders like `dev_openrouter_key`,
    empty strings, or `changeme` must never be promoted to primary+active —
    seeding a fake key was the source of the silent chat failures.
    """
    return bool(value) and value.startswith("sk-or-") and len(value) > 20


async def _seed_initial_api_key() -> None:
    """Ensure the env OpenRouter key is the primary+active row on startup.

    Upserts by key suffix: if a row for this key already exists it is promoted
    to primary+active (and re-encrypted in case it rotated); otherwise a new
    primary+active row is inserted. Any other key loses its primary flag. A
    placeholder env value is ignored so we never activate a fake key.
    """
    raw = settings.openrouter_default_api_key
    if not _is_real_openrouter_key(raw):
        logger.warning(
            "OPENROUTER_DEFAULT_API_KEY is missing or a placeholder — "
            "no primary key seeded; chat will return 503 until a real key "
            "is activated."
        )
        return

    suffix = raw[-8:]
    factory = get_session_factory()
    async with factory() as db:
        row = (
            await db.execute(select(ApiKey).where(ApiKey.key_suffix == suffix))
        ).scalar_one_or_none()

        # Demote any current primary so exactly one row stays primary.
        await db.execute(update(ApiKey).values(is_primary=False))

        if row is None:
            db.add(ApiKey(
                label="default",
                encrypted_key=encrypt_key(raw),
                key_suffix=suffix,
                status="active",
                is_primary=True,
            ))
        else:
            row.encrypted_key = encrypt_key(raw)
            row.status = "active"
            row.is_primary = True

        await db.commit()

    logger.info("OpenRouter primary key active: %s...", raw[:8])


async def _run_model_sync_once() -> None:
    """One model-catalogue sync with its own short-lived DB/Redis handles."""
    factory = get_session_factory()
    redis = Redis(connection_pool=get_redis_pool())
    try:
        async with factory() as db:
            result = await ModelSyncService().sync_models(db, redis)
        logger.info(
            "Model sync: +%d new, %d updated, %d deactivated",
            result.added,
            result.updated,
            result.deactivated,
        )
        for err in result.errors:
            logger.warning("Model sync error: %s", err)
    finally:
        await redis.aclose()


async def _model_sync_loop() -> None:
    """Background loop — re-sync the OpenRouter model catalogue every 24h.

    Errors are logged but never re-raised so a flaky upstream can't kill the loop.
    """
    while True:
        try:
            await asyncio.sleep(_MODEL_SYNC_INTERVAL_SECONDS)
            await _run_model_sync_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Periodic model sync failed: %s", exc)


async def _run_exchange_rate_once() -> None:
    """One CBR exchange-rate poll with its own short-lived DB/Redis handles.

    run_exchange_rate_update gates itself on the auto_exchange_rate_enabled flag
    and the interval throttle, so this is a cheap no-op until both allow it.
    """
    factory = get_session_factory()
    redis = Redis(connection_pool=get_redis_pool())
    try:
        async with factory() as db:
            await run_exchange_rate_update(db, redis)
    finally:
        await redis.aclose()


async def _exchange_rate_loop() -> None:
    """Background loop — poke the CBR updater every minute.

    Errors are logged but never re-raised so a flaky CBR upstream or DB blip
    can't kill the loop.
    """
    while True:
        try:
            await asyncio.sleep(_EXCHANGE_RATE_TICK_SECONDS)
            await _run_exchange_rate_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Periodic exchange-rate update failed: %s", exc)


async def _run_reservation_cleanup_once() -> None:
    """One every-minute cleanup tick on its own short-lived DB session.

    The in-process replacement for the host-cron `cleanup reservations` and
    `cleanup reconcile` jobs (scripts/setup_cron.sh): that crontab is never
    installed in the Docker deployment, so without this expired reservations
    were never released and balances stuck on leaked reservations (tr-u3y5).
    Neither task needs Redis — both take only a DB session. If the reaper raises,
    the reconcile is skipped this tick and both retry on the next one.
    """
    factory = get_session_factory()
    async with factory() as db:
        released = await release_expired_reservations(db)
        if released:
            logger.info("Released %d expired reservations", released)
        reconciled = await reconcile_partial_costs(db)
        if reconciled:
            logger.info("Reconciled %d partial OpenRouter costs", reconciled)
    # Heal any denormalised reserved-counter drift on a FRESH session — the reaper
    # above just changed SUM(active), and a separate session keeps this read clean
    # of the reaper's cached user rows (tr-12jl).
    async with factory() as db:
        healed = await reconcile_reserved_drift(db)
        if healed:
            logger.info("Reconciled reserved-counter drift for %d users", healed)


async def _reservation_cleanup_loop() -> None:
    """Background loop — every minute, release expired reservations and reconcile
    partial costs. The in-process owner of the every-minute cleanup tick (tr-u3y5).

    Errors are logged but never re-raised: a transient DB blip must never kill the
    reaper, because a dead reaper is exactly the keystone bug this loop fixes.
    """
    while True:
        try:
            await asyncio.sleep(_RESERVATION_CLEANUP_TICK_SECONDS)
            await _run_reservation_cleanup_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Periodic reservation cleanup failed: %s", exc)


async def _run_daily_reconciliation_safe() -> None:
    """One-shot financial reconciliation for startup, run as a fire-and-forget task.

    NOT awaited inline in lifespan: _check_balance_invariant is an unbounded full-table
    users⋈transactions aggregate, so awaiting it before `yield` could stall the leader
    worker's readiness (audit). A background task keeps startup fast while still firing
    the §12 safety net once per leader lifetime (restart-resilient). Errors only warn.
    """
    try:
        await run_daily_reconciliation()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Initial daily reconciliation failed: %s", exc)


async def _daily_reconciliation_loop() -> None:
    """Background loop — run the §12 financial reconciliation safety net every 24h.

    Host-cron-only before (scripts/setup_cron.sh, never installed by the Docker deploy),
    so the ledger safety net never ran (tr-376l). Running it in-process under the leader
    lock makes it actually fire. Mirrors _model_sync_loop.

    Errors are logged but never re-raised so a transient DB / Telegram blip can't kill
    the loop.
    """
    while True:
        try:
            await asyncio.sleep(_DAILY_RECONCILIATION_INTERVAL_SECONDS)
            await run_daily_reconciliation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Daily reconciliation failed: %s", exc)


async def _run_health_check_once() -> None:
    """One health probe with its own short-lived DB/Redis handles."""
    factory = get_session_factory()
    redis = Redis(connection_pool=get_redis_pool())
    try:
        async with factory() as db:
            await check_openrouter_health(db, redis)
    finally:
        await redis.aclose()


async def _health_check_loop() -> None:
    """Background loop — every health_check_interval_seconds, probe OpenRouter.

    Errors are logged but never re-raised: the loop must outlive a flaky
    upstream so the cached state eventually self-heals.
    """
    while True:
        try:
            await asyncio.sleep(settings.health_check_interval_seconds)
            await _run_health_check_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenRouter health check failed: %s", exc)


async def _try_acquire_leadership(redis: Redis) -> bool:
    """Win the background-task lock via SET NX. True → this worker is the leader."""
    try:
        return bool(
            await redis.set(
                _LEADER_LOCK_KEY, _WORKER_ID, nx=True, ex=_LEADER_LOCK_TTL_SECONDS
            )
        )
    except Exception as exc:  # noqa: BLE001 — Redis blip → behave as a follower
        # ERROR (not WARNING): a failed acquire means this worker runs no background tasks,
        # and under a Redis OOM (noeviction) the WHOLE fleet can go leaderless — monitoring
        # must see it (tr-5rd7). Self-healing follower re-election is a separate follow-up.
        logger.error("Leader election failed (%s); running as follower", exc)
        return False


async def _renew_leadership_loop(redis: Redis) -> None:
    """Keep the leader lock fresh while we still own it. Returns (does not
    raise) if we ever lose it (e.g. it expired and another worker took over) —
    the supervisor treats a normal return as a demotion signal."""
    while True:
        try:
            await asyncio.sleep(_LEADER_RENEW_INTERVAL_SECONDS)
            if await redis.get(_LEADER_LOCK_KEY) == _WORKER_ID:
                await redis.expire(_LEADER_LOCK_KEY, _LEADER_LOCK_TTL_SECONDS)
            else:
                logger.warning("Lost background-task leadership; stopping renewals")
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Leadership renewal failed: %s", exc)


async def _start_leader_tasks() -> list[asyncio.Task]:
    """Once-per-promotion initial work, then the periodic leader-only loops.

    Every initial step is best-effort — a network error or cold-DB blip only
    warns, it must never block the worker from serving traffic.
    """
    # Seed the primary OpenRouter key once: a placeholder/missing key only
    # warns, and doing it leader-only avoids N concurrent upserts racing on
    # the demote-all UPDATE.
    try:
        await _seed_initial_api_key()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Initial API key seed failed: %s", exc)

    try:
        await _run_model_sync_once()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Initial model sync failed: %s", exc)

    try:
        await _run_health_check_once()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Initial OpenRouter health check failed: %s", exc)

    # Sweep already-expired reservations immediately on promotion so a deploy
    # (or a failover) doesn't wait a full tick to release balances stuck on
    # leaked reservations (tr-u3y5).
    try:
        await _run_reservation_cleanup_once()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Initial reservation cleanup failed: %s", exc)

    return [
        # Startup reconciliation — never awaited inline (its scan is unbounded
        # and must not stall readiness); fires once per promotion.
        asyncio.create_task(_run_daily_reconciliation_safe()),
        asyncio.create_task(_health_check_loop()),
        asyncio.create_task(_model_sync_loop()),
        asyncio.create_task(_exchange_rate_loop()),
        asyncio.create_task(_reservation_cleanup_loop()),
        asyncio.create_task(_daily_reconciliation_loop()),
    ]


async def _leadership_supervisor(redis: Redis) -> None:
    """Acquire → lead → demote → retry, forever (tr-2ubv.24).

    The old design acquired ONCE at startup: a worker that booted while the
    previous leader's lock was still alive (SIGKILL leaves it for up to 60s),
    or while Redis was down/OOM, became a PERMANENT follower — with a single
    worker the whole deployment ran no background tasks until a lucky restart.
    Followers now retry every _FOLLOWER_RETRY_INTERVAL_SECONDS; a leader whose
    renewal loop reports the lock lost cancels its leader-only loops (no
    dual-leader window with the new lock holder) and rejoins the retry loop.
    """
    logged_follower = False
    while True:
        if await _try_acquire_leadership(redis):
            logged_follower = False
            logger.info("Worker %s is the background-task leader", _WORKER_ID)
            # Renew from the very start: the initial steps in _start_leader_tasks
            # can be slow (the reconciliation scan is unbounded), and if the 60s
            # TTL lapsed before the first renewal a second worker could win the
            # lock and dual-lead every loop (audit).
            renew = asyncio.create_task(_renew_leadership_loop(redis))
            leader_tasks: list[asyncio.Task] = []
            try:
                leader_tasks = await _start_leader_tasks()
                logger.info("Background schedulers started (leader)")
                # Returns normally only when leadership is lost; shutdown
                # arrives here as CancelledError.
                await renew
            finally:
                renew.cancel()
                for task in [renew, *leader_tasks]:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
            # ERROR: losing the lock while alive means clock/Redis trouble or a
            # competing leader — monitoring must see it (mirrors tr-5rd7).
            logger.error(
                "Worker %s lost background-task leadership; demoted to follower — "
                "will retry every %ss",
                _WORKER_ID,
                _FOLLOWER_RETRY_INTERVAL_SECONDS,
            )
        elif not logged_follower:
            # Log the follower state once per stretch, not every retry tick.
            logged_follower = True
            logger.info(
                "Worker %s is a follower; retrying leadership every %ss",
                _WORKER_ID,
                _FOLLOWER_RETRY_INTERVAL_SECONDS,
            )
        await asyncio.sleep(_FOLLOWER_RETRY_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    # Per-worker resources — every process needs its own DB/Redis pools.
    init_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )
    init_redis(settings.redis_url)

    # Exactly one worker runs the background tasks at a time; the supervisor
    # promotes/demotes this worker as the Redis lock comes and goes.
    redis = Redis(connection_pool=get_redis_pool())
    supervisor = asyncio.create_task(_leadership_supervisor(redis))

    try:
        yield
    finally:
        supervisor.cancel()
        try:
            await supervisor
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        # Release the lock on a clean shutdown so a redeploy re-elects quickly.
        try:
            if await redis.get(_LEADER_LOCK_KEY) == _WORKER_ID:
                await redis.delete(_LEADER_LOCK_KEY)
        except Exception:  # noqa: BLE001
            pass
        await redis.aclose()
        await dispose_engine()
        await close_redis()


def create_app() -> FastAPI:
    # In production the OpenAPI schema + Swagger/ReDoc UIs are disabled so the full
    # admin/financial endpoint map isn't handed to attackers (tr-uufc). openapi_url=None
    # also removes the raw schema, not just the two UIs.
    _docs_on = settings.app_env != "production"
    application = FastAPI(
        title="Trinity",
        version=settings.app_version,
        docs_url="/api/docs" if _docs_on else None,
        redoc_url="/api/redoc" if _docs_on else None,
        openapi_url="/openapi.json" if _docs_on else None,
        lifespan=lifespan,
    )

    # Added first → innermost: wraps just the route handler so the timeout
    # covers handler work, not the other middleware. SSE chat stream is exempt.
    application.add_middleware(RequestTimeoutMiddleware)
    # CSRF must sit INSIDE CORSMiddleware (added before it): its self-made
    # responses (403 invalid-token, 503 Redis-down) then traverse CORS and get
    # Access-Control-Allow-Origin — otherwise the browser reports an opaque
    # net::ERR_FAILED and the frontend can't show the server's RU message
    # (tr-w4fy.2, same class as tr-yxki). Preflight OPTIONS is still answered
    # at the CORS layer without touching CSRF.
    application.add_middleware(CSRFMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Added last → outermost: reject oversized bodies before any other work.
    application.add_middleware(BodySizeLimitMiddleware)

    @application.exception_handler(TrinityError)
    async def trinity_error_handler(request: Request, exc: TrinityError) -> JSONResponse:
        # Retryable errors (rate limit, transient service-unavailable) may carry a
        # retry_after hint — surface it as a Retry-After header so clients back off
        # correctly instead of hammering.
        retry_after = getattr(exc, "retry_after", None)
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
            headers=headers,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # tr-azhb: exc.errors() can embed a raw exception instance in ctx.error
        # (any field/model validator that does `raise ValueError(...)` — the
        # idiomatic Pydantic v2 pattern). json.dumps() can't serialize that, so
        # without jsonable_encoder() this crashes INSIDE the handler and the
        # intended 422 is masked as an opaque 500 by the catch-all. FastAPI's
        # own default handler avoids this the same way.
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "validation_error", "message": "Ошибка валидации данных", "details": jsonable_encoder(exc.errors())}},
        )

    @application.exception_handler(SQLAlchemyTimeoutError)
    async def db_pool_timeout_handler(
        request: Request, exc: SQLAlchemyTimeoutError
    ) -> JSONResponse:
        # Connection pool exhausted (pool_timeout exceeded) — the server is
        # saturated, not broken. 503 lets clients retry rather than treating it
        # as a hard 500 (load-test bottleneck B3). A pooled connection usually
        # frees within a second or two, so a short Retry-After.
        return overloaded_response("pool_timeout", retry_after=2)

    @application.exception_handler(_RedisConnectionError)
    async def redis_unavailable_handler(
        request: Request, exc: _RedisConnectionError
    ) -> JSONResponse:
        # tr-8dy3: Redis connection pool exhausted / server unreachable (MaxConnectionsError
        # is a ConnectionError). Defense-in-depth backstop for any Redis call not locally
        # fail-open'd — degrade to a graceful 503+Retry-After instead of a 500. Scoped to
        # ConnectionError (not the broad RedisError) so a data/command error isn't masked.
        logger.warning("Redis unavailable on %s: %s", request.url.path, exc)
        return overloaded_response("redis_unavailable", retry_after=2)

    @application.exception_handler(_RedisTimeoutError)
    async def redis_timeout_handler(
        request: Request, exc: _RedisTimeoutError
    ) -> JSONResponse:
        # tr-8dy3: redis.TimeoutError is a RedisError but NOT a ConnectionError and NOT the
        # builtin TimeoutError, so it would otherwise miss both the handler above and the
        # io_timeout handler below and 500. A slow/overloaded Redis op → graceful 503 too.
        # (ResponseError — a data/command bug — is deliberately NOT caught, so it stays a 500.)
        logger.warning("Redis command timeout on %s: %s", request.url.path, exc)
        return overloaded_response("redis_timeout", retry_after=2)

    @application.exception_handler(TimeoutError)
    async def asyncio_timeout_handler(
        request: Request, exc: TimeoutError
    ) -> JSONResponse:
        # asyncio.TimeoutError is builtin TimeoutError on 3.11+. Surfaces when an
        # awaited I/O op exceeds its deadline under overload → 503, not 500.
        return overloaded_response("io_timeout", retry_after=5)

    _cors_allowed_origins = {o.strip() for o in settings.cors_origins.split(",")}

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # Starlette installs a handler registered for the bare Exception class into
        # ServerErrorMiddleware, which wraps OUTSIDE every app.add_middleware() layer
        # (including CORSMiddleware above) -- so this response never passes through
        # CORSMiddleware and must attach its own CORS headers, or the browser blocks
        # the frontend from reading the body and reports an opaque CORS/network
        # failure instead of this RU error message (tr-yxki).
        response = JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "Внутренняя ошибка сервера", "details": None}},
        )
        origin = request.headers.get("origin")
        if origin in _cors_allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
        return response

    application.include_router(api_v1_router, prefix="/api/v1")

    return application


app = create_app()
