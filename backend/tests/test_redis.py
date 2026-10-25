import asyncio

import pytest
from redis.asyncio import ConnectionPool, Redis

from tests.conftest import TEST_REDIS_URL


async def test_redis_connection_creates():
    pool = ConnectionPool.from_url(TEST_REDIS_URL, decode_responses=True)
    assert pool is not None
    await pool.disconnect()


async def test_redis_ping(redis_client):
    result = await redis_client.ping()
    assert result is True


async def test_redis_set_get(redis_client):
    await redis_client.set("test:set_get", "hello")
    value = await redis_client.get("test:set_get")
    assert value == "hello"
    await redis_client.delete("test:set_get")


async def test_redis_connection_cleanup():
    pool = ConnectionPool.from_url(TEST_REDIS_URL, decode_responses=True)
    client = Redis(connection_pool=pool)
    await client.set("test:cleanup", "value")
    await client.aclose()
    # Pool is still usable after client close
    client2 = Redis(connection_pool=pool)
    assert await client2.ping() is True
    await client2.delete("test:cleanup")
    await client2.aclose()
    await pool.disconnect()


async def test_multiple_concurrent_redis_ops(redis_pool):
    async def one_op(i: int) -> str | None:
        client = Redis(connection_pool=redis_pool)
        key = f"test:concurrent:{i}"
        await client.set(key, f"value_{i}")
        value = await client.get(key)
        await client.delete(key)
        await client.aclose()
        return value

    results = await asyncio.gather(*[one_op(i) for i in range(10)])
    assert all(results[i] == f"value_{i}" for i in range(10))


async def test_redis_invalid_url():
    pool = ConnectionPool.from_url("redis://localhost:19999/0", socket_connect_timeout=2)
    client = Redis(connection_pool=pool)
    with pytest.raises(Exception):
        await client.ping()
    await client.aclose()
    await pool.disconnect()


async def test_redis_key_expiry(redis_client):
    await redis_client.set("test:expiry", "value", ex=1)
    assert await redis_client.get("test:expiry") == "value"
    await asyncio.sleep(1.5)
    assert await redis_client.get("test:expiry") is None


async def test_redis_pipeline_operations(redis_client):
    pipe = redis_client.pipeline()
    pipe.set("test:pipe:1", "alpha")
    pipe.set("test:pipe:2", "beta")
    pipe.get("test:pipe:1")
    pipe.get("test:pipe:2")
    results = await pipe.execute()
    assert results[0] is True
    assert results[1] is True
    assert results[2] == "alpha"
    assert results[3] == "beta"
    await redis_client.delete("test:pipe:1", "test:pipe:2")
