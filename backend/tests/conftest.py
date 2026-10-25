import os
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import ConnectionPool, Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# backend/ — alembic.ini and the app package live here.
_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _inside_docker() -> bool:
    """True when pytest is running inside a Docker container."""
    return Path("/.dockerenv").exists()


def _default_redis_url() -> str:
    host = "redis" if _inside_docker() else "localhost"
    return f"redis://{host}:6379/0"


def _require_test_db_url() -> str:
    """Resolve the test DB URL, failing loudly rather than ever touching dev data.

    There is deliberately no fallback to the dev `trinity` database: the test
    cleanup fixtures issue destructive DELETEs, so silently running them against
    dev would wipe real data. TEST_DATABASE_URL is supplied by docker-compose
    (container) or pytest-env (host) — if it is missing, something is misconfigured.
    """
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "TEST_DATABASE_URL is not set. Tests must run against the isolated "
            "'trinity_test' database, never the dev 'trinity' DB. It is provided "
            "by the backend service in docker-compose.yml and by pytest-env in "
            "pyproject.toml for host runs — check your setup."
        )
    db_name = url.rsplit("/", 1)[-1]
    if not db_name.startswith("trinity_test"):
        raise RuntimeError(
            f"TEST_DATABASE_URL points at {db_name!r}, not the 'trinity_test' "
            "database. Refusing to run destructive test cleanup against it."
        )
    return url


# User-data tables wiped between tests, in FK-safe order (children before
# parents). Migration-seeded reference data is intentionally preserved:
# system_settings (001) and model_pricing (002) are treated as fixtures the
# suite relies on, and alembic_version must never be touched.
_WIPE_TABLES: tuple[str, ...] = (
    "usage_logs",
    "chat_images",
    "chat_files",
    "messages",
    "conversations",
    "reservations",
    "transactions",
    "notifications",
    "alerts",
    "admin_audit_log",
    "refresh_tokens",
    "password_reset_tokens",
    "api_keys",
    "users",
)

TEST_DB_URL: str = _require_test_db_url()
TEST_REDIS_URL: str = os.getenv("TEST_REDIS_URL", _default_redis_url())


@pytest.fixture(scope="session", autouse=True)
def apply_migrations():
    """Bring trinity_test up to head via Alembic before the test session.

    Runs `alembic upgrade head` in a subprocess with DATABASE_URL pointed at the
    test DB (alembic/env.py reads settings.database_url, which pydantic sources
    from DATABASE_URL). Idempotent: re-running on an already-migrated DB is a no-op.
    """
    # DATABASE_URL_DIRECT is set in the backend container (for the app's Alembic
    # to bypass PgBouncer against the DEV db). Blank it here so the test-suite
    # migration uses DATABASE_URL (the trinity_test db) and never the dev db —
    # alembic/env.py prefers database_url_direct only when it's non-empty.
    env = {**os.environ, "DATABASE_URL": TEST_DB_URL, "DATABASE_URL_DIRECT": ""}
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Failed to apply migrations to the test database "
            f"({TEST_DB_URL}):\n{result.stdout}\n{result.stderr}"
        )
    yield


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(TEST_DB_URL, pool_size=5, max_overflow=10, pool_pre_ping=True)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_database(engine):
    """Wipe user data before each test for isolation, preserving seed reference data.

    DELETE (not TRUNCATE) is required: system_settings.updated_by has an FK to
    users, so `TRUNCATE users CASCADE` would also wipe the seeded settings, and a
    non-cascading TRUNCATE would error on that constraint. Ordered DELETEs plus
    nulling updated_by sidestep both problems while keeping system_settings and
    model_pricing intact.
    """
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE system_settings SET updated_by = NULL"))
        for table in _WIPE_TABLES:
            await conn.execute(text(f"DELETE FROM {table}"))
    yield


@pytest_asyncio.fixture
async def db_session(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def app_session_factory():
    """Initialize the app's global engine/session factory against the test DB.

    Tests that drive `process_chat_request` directly (not through the ASGI app)
    and exercise the abort cleanup need this: the cleanup finalizes/releases on a
    FRESH session from `get_session_factory()` (tr-04a — never the request-scoped
    connection, which FastAPI tears down concurrently), so the global factory must
    be initialized or it raises "Session factory not initialized". Uses its own
    pool against the same `trinity_test` DB, mirroring production where the cleanup
    connection is distinct from the request connection.
    """
    from app.core.database import dispose_engine, init_engine

    init_engine(TEST_DB_URL, pool_size=5, max_overflow=10)
    yield
    await dispose_engine()


@pytest_asyncio.fixture
async def redis_pool():
    pool = ConnectionPool.from_url(TEST_REDIS_URL, decode_responses=True)
    yield pool
    await pool.disconnect()


@pytest_asyncio.fixture
async def redis_client(redis_pool):
    client = Redis(connection_pool=redis_pool)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def http_client():
    """FastAPI test client with DB, Redis, and CSRF token pre-bootstrapped."""
    from app.core.database import dispose_engine, init_engine
    from app.core.redis import close_redis, init_redis
    from app.main import app

    init_engine(TEST_DB_URL, pool_size=10, max_overflow=20)
    init_redis(TEST_REDIS_URL)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Trigger CSRF token generation; store it as default header for all requests.
        resp = await client.get("/api/v1/health")
        client.headers["X-CSRF-Token"] = resp.headers.get("X-CSRF-Token", "")
        yield client

    await dispose_engine()
    await close_redis()


@pytest_asyncio.fixture
async def csrf_token(http_client: AsyncClient) -> str:
    """Obtain the current CSRF token via GET /health."""
    response = await http_client.get("/api/v1/health")
    return response.headers["X-CSRF-Token"]
