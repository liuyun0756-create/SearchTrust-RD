"""
app/core/redis_client.py
────────────────────────
Async Redis connection-pool manager.

Usage
-----
    from app.core.redis_client import get_redis, RedisClient

    # Inside an async function
    redis = get_redis()
    await redis.set("key", "value", ttl=60)
    value = await redis.get("key")
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Union

import orjson
import redis.asyncio as aioredis
from redis.asyncio import ConnectionPool
from redis.exceptions import RedisError

from app.core.config import settings

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Internal pool singleton
# ─────────────────────────────────────────────────────────────────────────────
_pool: Optional[ConnectionPool] = None


def _get_pool() -> ConnectionPool:
    """
    Return (or lazily create) the async connection pool bound to the
    *current* running event loop.

    Each call checks whether the existing pool's internal loop matches the
    loop that is currently running.  If they differ (e.g. a Celery worker
    that calls asyncio.run() creates a brand-new loop each time), the old
    pool is discarded and a fresh one is built for the new loop.
    """
    global _pool
    import asyncio

    if _pool is not None:
        # Detect loop mismatch: pool was created for a different (possibly
        # already-destroyed) loop.  asyncio.get_event_loop() returns the
        # *running* loop inside asyncio.run(), so compare by identity.
        try:
            running_loop = asyncio.get_running_loop()
            pool_loop = getattr(_pool, "_available_connections", None)
            # Simpler heuristic: if the running loop is not the one stored
            # in the pool's connection objects, reset.
            # We check via the internal _loop attribute that aioredis sets.
            existing_conn = None
            if hasattr(_pool, "_created_connections"):
                conns = list(_pool._created_connections)
                if conns:
                    existing_conn = conns[0]
            if existing_conn is not None:
                conn_loop = getattr(existing_conn, "_loop", None)
                if conn_loop is not None and conn_loop is not running_loop:
                    logger.debug("Redis pool loop mismatch — resetting pool for new event loop")
                    _pool = None
        except RuntimeError:
            # No running loop yet (called from sync context before asyncio.run)
            pass

    if _pool is None:
        _pool = aioredis.ConnectionPool.from_url(
            settings.REDIS_URL,
            max_connections=50,
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=10,
            retry_on_timeout=True,
        )
    return _pool


def reset_pool() -> None:
    """
    Discard the current connection pool without closing it.

    Call this **before** ``asyncio.run()`` in Celery tasks so that the new
    event loop gets a fresh pool rather than inheriting one bound to the
    FastAPI / previous loop.  The old pool's connections will be GC'd.
    """
    global _pool
    _pool = None
    logger.debug("Redis connection pool reset (new event loop will be used)")


async def close_pool() -> None:
    """Gracefully close the connection pool (call on app shutdown)."""
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
        logger.info("Redis connection pool closed")


# ─────────────────────────────────────────────────────────────────────────────
# RedisClient wrapper
# ─────────────────────────────────────────────────────────────────────────────
class RedisClient:
    """
    Thin async wrapper around redis.asyncio.Redis.

    All methods catch RedisError so callers don't need scattered try/except
    blocks for cache misses — returning None on failure degrades gracefully.
    """

    def __init__(self) -> None:
        self._redis: aioredis.Redis = aioredis.Redis(connection_pool=_get_pool())

    # ── Basic string ops ──────────────────────────────────────────────────────

    async def get(self, key: str) -> Optional[str]:
        """
        Retrieve a string value from Redis.

        Returns None if the key does not exist or on any Redis error.
        """
        try:
            value: Optional[str] = await self._redis.get(key)
            return value
        except RedisError as exc:
            logger.warning("Redis GET failed for key=%s: %s", key, exc)
            return None

    async def set(
        self,
        key: str,
        value: str,
        ttl: Optional[int] = None,
    ) -> bool:
        """
        Store a string value in Redis with an optional TTL (seconds).

        Returns True on success, False on error.
        """
        try:
            await self._redis.set(key, value, ex=ttl)
            return True
        except RedisError as exc:
            logger.warning("Redis SET failed for key=%s: %s", key, exc)
            return False

    async def delete(self, key: str) -> int:
        """
        Delete a key from Redis.

        Returns the number of keys removed (0 or 1).
        """
        try:
            return await self._redis.delete(key)
        except RedisError as exc:
            logger.warning("Redis DELETE failed for key=%s: %s", key, exc)
            return 0

    async def exists(self, key: str) -> bool:
        """Return True if *key* exists in Redis."""
        try:
            result: int = await self._redis.exists(key)
            return bool(result)
        except RedisError as exc:
            logger.warning("Redis EXISTS failed for key=%s: %s", key, exc)
            return False

    async def expire(self, key: str, ttl: int) -> bool:
        """Reset the TTL of an existing key."""
        try:
            return bool(await self._redis.expire(key, ttl))
        except RedisError as exc:
            logger.warning("Redis EXPIRE failed for key=%s: %s", key, exc)
            return False

    async def ttl(self, key: str) -> int:
        """Return remaining TTL in seconds (-2 = not found, -1 = no TTL)."""
        try:
            return await self._redis.ttl(key)
        except RedisError as exc:
            logger.warning("Redis TTL failed for key=%s: %s", key, exc)
            return -2

    # ── JSON helpers ──────────────────────────────────────────────────────────

    async def get_json(self, key: str) -> Optional[Any]:
        """
        Retrieve and JSON-decode a stored value.

        Returns None if the key does not exist or the value is not valid JSON.
        """
        raw = await self.get(key)
        if raw is None:
            return None
        try:
            return orjson.loads(raw)
        except (orjson.JSONDecodeError, TypeError) as exc:
            logger.warning("Redis JSON decode failed for key=%s: %s", key, exc)
            return None

    async def set_json(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
    ) -> bool:
        """
        JSON-encode *value* and store it in Redis.

        Returns True on success, False on error.
        """
        try:
            # orjson.dumps returns bytes; decode to str for redis string storage
            serialised = orjson.dumps(value, option=orjson.OPT_NON_STR_KEYS).decode()
        except (TypeError, orjson.JSONEncodeError) as exc:
            logger.error("JSON serialisation failed for key=%s: %s", key, exc)
            return False
        return await self.set(key, serialised, ttl=ttl)

    # ── Atomic increment ──────────────────────────────────────────────────────

    async def incr(self, key: str, amount: int = 1) -> Optional[int]:
        """Atomically increment a counter; returns the new value."""
        try:
            return await self._redis.incrby(key, amount)
        except RedisError as exc:
            logger.warning("Redis INCR failed for key=%s: %s", key, exc)
            return None

    async def incr_with_ttl(self, key: str, ttl: int, amount: int = 1) -> Optional[int]:
        """
        Atomically increment a counter and set its TTL in a single pipeline.

        Unlike calling incr() + expire() separately, this method sends both
        commands in one round-trip and the TTL is always applied — eliminating
        the race condition where the key could become immortal if the process
        crashes between the two calls.

        Returns the new counter value, or None on Redis error.
        """
        try:
            pipe = self._redis.pipeline()
            pipe.incrby(key, amount)
            pipe.expire(key, ttl)
            results = await pipe.execute()
            # results = [new_count (int), expire_result (bool)]
            return results[0]
        except RedisError as exc:
            logger.warning("Redis INCR_WITH_TTL failed for key=%s: %s", key, exc)
            return None

    # ── Health check ──────────────────────────────────────────────────────────

    async def ping(self) -> bool:
        """Return True if Redis responds to PING."""
        try:
            return await self._redis.ping()
        except RedisError:
            return False

    # ── Context manager support ───────────────────────────────────────────────

    async def __aenter__(self) -> "RedisClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass  # pool is shared; individual client needs no close


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton accessor
# ─────────────────────────────────────────────────────────────────────────────

def get_redis() -> RedisClient:
    """
    Return a RedisClient bound to the current event loop.

    Delegates pool management to _get_pool() which detects loop mismatches
    and resets the pool when needed (e.g. inside Celery asyncio.run() calls).
    """
    return RedisClient()


async def init_redis() -> None:
    """
    Eagerly initialise the Redis connection pool and verify connectivity.

    Call this in the FastAPI `startup` event handler so connection errors
    surface at boot time rather than on the first request.
    """
    _ = _get_pool()          # create pool
    try:
        client = get_redis()
        await client.ping()
        logger.info("Redis connection pool initialised — URL: %s", settings.REDIS_URL)
    except Exception as exc:
        raise RuntimeError(
            f"Cannot connect to Redis at {settings.REDIS_URL}"
        ) from exc
