import asyncio
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.core import database as db_module
from tests.conftest import TEST_DB_URL


async def test_engine_creates_successfully():
    engine = create_async_engine(TEST_DB_URL, pool_size=2, max_overflow=0)
    assert engine is not None
    await engine.dispose()


async def test_session_factory_works():
    engine = create_async_engine(TEST_DB_URL, pool_size=2)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        assert isinstance(session, AsyncSession)
    await engine.dispose()


async def test_database_connectivity(engine):
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar() == 1


async def test_session_cleanup(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(text("SELECT 1"))
    # Connection returned to pool — pool has 0 checked-out connections
    assert engine.sync_engine.pool.checkedout() == 0


async def test_multiple_concurrent_sessions(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def one_query() -> int:
        async with factory() as session:
            result = await session.execute(text("SELECT 1"))
            return result.scalar()

    results = await asyncio.gather(*[one_query() for _ in range(10)])
    assert all(r == 1 for r in results)


async def test_engine_dispose():
    engine = create_async_engine(TEST_DB_URL, pool_size=2)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    await engine.dispose()
    # Pool reset — no connections checked out
    assert engine.sync_engine.pool.checkedout() == 0


async def test_invalid_database_url():
    bad_engine = create_async_engine(
        "postgresql+asyncpg://baduser:badpass@localhost:19999/baddb",
        connect_args={"timeout": 2},
    )
    with pytest.raises(Exception):
        async with bad_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    await bad_engine.dispose()


async def test_pool_size_respected():
    pool_size = 3
    engine = create_async_engine(TEST_DB_URL, pool_size=pool_size, max_overflow=0)
    assert engine.sync_engine.pool.size() == pool_size
    await engine.dispose()


def test_pool_defaults_are_bumped():
    # tr-4fy.3: per-worker pool raised 20/10 -> 25/15 for 1000+ concurrent.
    # Assert the CODE default (via model_fields) so an env override in a
    # load-test shell can't mask a silent revert of the shipped default.
    assert Settings.model_fields["database_pool_size"].default == 25
    assert Settings.model_fields["database_max_overflow"].default == 15


def test_init_engine_forwards_pool_sizing():
    # Wiring guard: init_engine must pass pool_size/max_overflow straight
    # through to create_async_engine, so the config bump actually reaches
    # every worker's engine.
    saved_engine = db_module._engine
    saved_factory = db_module._session_factory
    try:
        with patch.object(db_module, "create_async_engine", return_value=MagicMock()) as m:
            db_module.init_engine("postgresql+asyncpg://x/y", pool_size=25, max_overflow=15)
        _, kwargs = m.call_args
        assert kwargs["pool_size"] == 25
        assert kwargs["max_overflow"] == 15
    finally:
        # init_engine unconditionally overwrites BOTH globals with the mock;
        # restore the pre-existing values (hermetic — no ordering dependency).
        db_module._engine = saved_engine
        db_module._session_factory = saved_factory


def test_init_engine_requires_explicit_pool_args():
    # tr-4fy.3: init_engine has NO signature defaults — Settings is the single
    # source of truth. A caller omitting pool args must fail loudly (TypeError),
    # never silently under-provision with a stale hardcoded default.
    with pytest.raises(TypeError):
        db_module.init_engine("postgresql+asyncpg://x/y")
