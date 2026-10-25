import asyncio

from redis.asyncio import ConnectionPool
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import TEST_DB_URL


async def test_health_returns_healthy_when_all_ok(http_client):
    response = await http_client.get("/api/v1/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["checks"]["database"] == "ok"
    assert data["checks"]["redis"] == "ok"


async def test_health_returns_503_when_db_down(http_client, monkeypatch):
    import app.core.database as db_module

    bad_engine = create_async_engine(
        "postgresql+asyncpg://bad:bad@localhost:19999/bad",
        connect_args={"timeout": 2},
    )
    monkeypatch.setattr(db_module, "_engine", bad_engine)

    response = await http_client.get("/api/v1/health")

    await bad_engine.dispose()

    assert response.status_code == 503
    data = response.json()
    assert data["checks"]["database"] == "error"
    assert data["checks"]["redis"] == "ok"


async def test_health_returns_503_when_redis_down(http_client, monkeypatch):
    import app.core.redis as redis_module

    bad_pool = ConnectionPool.from_url("redis://localhost:19999/0", socket_connect_timeout=2)
    monkeypatch.setattr(redis_module, "_pool", bad_pool)

    response = await http_client.get("/api/v1/health")

    await bad_pool.disconnect()

    assert response.status_code == 503
    data = response.json()
    assert data["checks"]["database"] == "ok"
    assert data["checks"]["redis"] == "error"


async def test_health_with_both_down(http_client, monkeypatch):
    import app.core.database as db_module
    import app.core.redis as redis_module

    bad_engine = create_async_engine(
        "postgresql+asyncpg://bad:bad@localhost:19999/bad",
        connect_args={"timeout": 2},
    )
    bad_pool = ConnectionPool.from_url("redis://localhost:19999/0", socket_connect_timeout=2)
    monkeypatch.setattr(db_module, "_engine", bad_engine)
    monkeypatch.setattr(redis_module, "_pool", bad_pool)

    response = await http_client.get("/api/v1/health")

    await bad_engine.dispose()
    await bad_pool.disconnect()

    assert response.status_code == 503
    data = response.json()
    assert data["checks"]["database"] == "error"
    assert data["checks"]["redis"] == "error"
    assert data["status"] == "unhealthy"


async def test_health_check_100_times(http_client):
    for _ in range(100):
        response = await http_client.get("/api/v1/health")
        assert response.status_code == 200
    # Reaching here means no connection leaks crashed the pool
    import app.core.database as db_module
    engine = db_module.get_engine()
    assert engine.sync_engine.pool.checkedout() == 0


async def test_health_concurrent_100_requests(http_client):
    async def one_request() -> int:
        response = await http_client.get("/api/v1/health")
        return response.status_code

    results = await asyncio.gather(*[one_request() for _ in range(100)])
    assert all(r == 200 for r in results)
