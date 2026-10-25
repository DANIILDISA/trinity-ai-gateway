import uuid

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str, pool_size: int, max_overflow: int) -> None:
    # pool_size/max_overflow are REQUIRED (no defaults, tr-4fy.3): Settings is the
    # single source of truth for pool sizing. A hardcoded default here would be a
    # second, silently-drifting source (it was stale at 20/10 after the 25/15 bump).
    # Every caller already passes settings.database_pool_size/max_overflow explicitly.
    global _engine, _session_factory
    # Local import avoids any import cycle; config has no DB dependency.
    from app.config import settings

    _engine = create_async_engine(
        database_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        # Fail fast when the pool is exhausted instead of queuing until the
        # request times out at 31s. A SQLAlchemy TimeoutError after this many
        # seconds is mapped to a clean 503 by the app exception handler
        # (load-test bottleneck B3). Configurable via settings (tr-3hs).
        pool_timeout=settings.database_pool_timeout,
        # PgBouncer transaction-pooling compatibility: asyncpg's prepared
        # statements break when a server connection is shared across transactions,
        # so we disable the statement cache and give every prepared statement a
        # unique name. Harmless against a direct Postgres connection (just no
        # caching), so it's applied unconditionally. pool_pre_ping is safe with
        # transaction mode as of SQLAlchemy 2.0.
        connect_args={
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
        },
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database engine not initialized")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Session factory not initialized")
    return _session_factory
