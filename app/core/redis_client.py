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
from typing import Any, Optional

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
    Return (or lazily create) the shared async connection pool.

    In the new asyncio-native Celery worker model, there is exactly one
    event loop per worker process and it is never replaced — so the pool
    is created once and reused for the lifetime of the process.
    """
    global _pool
    if _pool is None:
        _pool = aioredis.ConnectionPool.from_url(
            settings.REDIS_URL,
            max_connections=20,
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=10,
            retry_on_timeout=True,
            health_check_interval=60,
        )
    return _pool


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
        Atomically increment a counter and set its TTL **only on first creation**.

        Implemented as a Lua script so the INCRBY + conditional EXPIRE execute
        atomically on the Redis server in a single round-trip — no pipeline
        race condition, no repeated TTL reset.

        Window semantics (fixed window):
          - The TTL is set exactly once, when the key is first created (INCRBY
            returns `amount`).  Subsequent requests within the same window do
            NOT extend the TTL, so the window always expires at a fixed point.
          - Using `val == amount` (instead of `val == 1`) correctly handles
            callers that pass amount > 1 (e.g. weighted / batch billing).

        Returns the new counter value, or None on Redis error.
        """
        _SCRIPT = """
local val = redis.call('INCRBY', KEYS[1], ARGV[2])
if val == tonumber(ARGV[2]) then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return val
"""
        try:
            result = await self._redis.eval(
                _SCRIPT,
                1,       # numkeys
                key,     # KEYS[1]
                ttl,     # ARGV[1]
                amount,  # ARGV[2]
            )
            return int(result)
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
    Return a RedisClient backed by the shared connection pool.

    The pool is created once per process and reused for the lifetime of
    the worker — safe because the asyncio-native Celery worker model
    maintains a single event loop per process.
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
