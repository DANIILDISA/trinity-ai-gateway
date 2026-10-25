from redis.asyncio import ConnectionPool, Redis


_pool: ConnectionPool | None = None
# One process-wide Redis client built on the shared pool. Reused everywhere
# instead of constructing a new Redis() per request (the CSRF middleware used to
# do that on every call) — a Redis client is safe for concurrent use and checks
# connections out of the pool per command (load-test bottleneck B1/Redis).
_client: Redis | None = None


def init_redis(redis_url: str) -> None:
    global _pool, _client
    # Cap concurrent connections so a traffic burst can't open an unbounded
    # number of sockets to Redis (the pool was previously uncapped). Sized via
    # settings.redis_max_connections (tr-8dy3) so bursts don't exhaust the pool.
    from app.config import settings

    _pool = ConnectionPool.from_url(
        redis_url, decode_responses=True, max_connections=settings.redis_max_connections
    )
    _client = Redis(connection_pool=_pool)


async def close_redis() -> None:
    global _pool, _client
    if _client is not None:
        await _client.aclose()
        _client = None
    if _pool is not None:
        await _pool.disconnect()
        _pool = None


def get_redis_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("Redis pool not initialized")
    return _pool


def get_redis_client() -> Redis:
    """The shared, app-lifetime Redis client. Never aclose() this per request."""
    if _client is None:
        raise RuntimeError("Redis client not initialized")
    return _client
