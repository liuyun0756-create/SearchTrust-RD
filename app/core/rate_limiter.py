"""
app/core/rate_limiter.py
────────────────────────
Distributed token-bucket rate limiter backed by Redis.

All Celery workers share the same bucket via an atomic Lua script, so the
global RPM cap is enforced across every process — not just within one.

Architecture
------------
- One Lua script combines "refill + consume" in a single round-trip.
- Callers block (async or sync) until a token is available or the
  ``max_wait`` deadline expires.
- Two flavours are provided:
    - ``async_acquire()``  — for asyncio contexts (FastAPI, dify_client)
    - ``sync_acquire()``   — for Celery worker tasks (synchronous code)

Usage
-----
    from app.core.rate_limiter import dify_rate_limiter

    # Inside an async function:
    await dify_rate_limiter.async_acquire()

    # Inside a Celery task (sync):
    dify_rate_limiter.sync_acquire()

Global instance
---------------
``dify_rate_limiter`` is pre-configured from ``settings`` and ready to use.
Adjust ``DIFY_RPM_CAPACITY / DIFY_RPM_REFILL / DIFY_RPM_INTERVAL`` in .env
to match your actual OpenAI / Dify quota.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import redis as sync_redis_lib
import redis.asyncio as aioredis

from app.core.config import settings
from app.core.redis_client import _get_pool

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Atomic Lua script
# ─────────────────────────────────────────────────────────────────────────────
#
# KEYS[1] = bucket key  (e.g. "rate_limit:dify")
# ARGV[1] = capacity    (max tokens)
# ARGV[2] = refill_amt  (tokens added per interval)
# ARGV[3] = refill_int  (interval in seconds)
# ARGV[4] = now         (current epoch float as string)
# ARGV[5] = cost        (tokens consumed per call, usually 1)
#
# Returns: 1 → token acquired, 0 → not enough tokens
#
_LUA_TOKEN_BUCKET = """
local key        = KEYS[1]
local capacity   = tonumber(ARGV[1])
local refill_amt = tonumber(ARGV[2])
local refill_int = tonumber(ARGV[3])
local now        = tonumber(ARGV[4])
local cost       = tonumber(ARGV[5])

local vals      = redis.call('HMGET', key, 'tokens', 'last_time')
local tokens    = tonumber(vals[1])
local last_time = tonumber(vals[2])

-- Initialise bucket on first call
if tokens == nil then
    tokens    = capacity
    last_time = now
end

-- Refill based on elapsed time
local elapsed = now - last_time
local refill  = math.floor(elapsed / refill_int) * refill_amt
if refill > 0 then
    tokens    = math.min(capacity, tokens + refill)
    last_time = now
end

-- Try to consume
if tokens >= cost then
    tokens = tokens - cost
    redis.call('HMSET', key, 'tokens', tokens, 'last_time', last_time)
    redis.call('EXPIRE', key, refill_int * 2)
    return 1
else
    redis.call('HMSET', key, 'tokens', tokens, 'last_time', last_time)
    redis.call('EXPIRE', key, refill_int * 2)
    return 0
end
"""


# ─────────────────────────────────────────────────────────────────────────────
# DistributedRateLimiter
# ─────────────────────────────────────────────────────────────────────────────

class DistributedRateLimiter:
    """
    Cross-process token-bucket rate limiter backed by Redis.

    Parameters
    ----------
    key:
        Redis hash key for this bucket (e.g. ``"rate_limit:dify"``).
    capacity:
        Maximum number of tokens the bucket can hold (burst ceiling).
    refill_amount:
        Tokens added every ``refill_interval`` seconds.
    refill_interval:
        Seconds between refills.
    max_wait:
        Maximum seconds ``acquire`` will block before raising ``TimeoutError``.
    retry_interval:
        Seconds between polling attempts when the bucket is empty.
    """

    def __init__(
        self,
        key: str,
        capacity: int,
        refill_amount: int,
        refill_interval: int,
        max_wait: float = 120.0,
        retry_interval: float = 1.0,
    ) -> None:
        self.key = key
        self.capacity = capacity
        self.refill_amount = refill_amount
        self.refill_interval = refill_interval
        self.max_wait = max_wait
        self.retry_interval = retry_interval

        # Lazy-initialised clients
        self._async_client: Optional[aioredis.Redis] = None
        self._sync_client: Optional[sync_redis_lib.Redis] = None

        # Cached registered Lua scripts (one per client instance)
        self._async_script: Optional[aioredis.client.Script] = None  # type: ignore[name-defined]
        self._sync_script: Optional[sync_redis_lib.client.Script] = None  # type: ignore[name-defined]

    # ── Async path (FastAPI / asyncio) ────────────────────────────────────────

    def _get_async_client(self) -> aioredis.Redis:
        # Reuse the global connection pool managed by redis_client.py.
        # Previously this called aioredis.from_url() on every acquire(),
        # which created a brand-new TCP connection each time instead of
        # drawing from the shared pool — wasteful under concurrency.
        #
        # _get_pool() already handles event-loop mismatch detection for
        # Celery workers, so we don't need any extra logic here.
        return aioredis.Redis(connection_pool=_get_pool())

    async def async_acquire(self, cost: int = 1) -> None:
        """
        Async token acquisition.  Blocks until a token is available or
        ``max_wait`` seconds have elapsed.

        Raises
        ------
        TimeoutError
            When no token can be acquired within ``max_wait`` seconds.
        """
        client = self._get_async_client()
        # Cache the registered script on the instance so we don't re-register
        # it on every call.  register_script() itself is cheap, but it creates
        # a new Script object and prevents any future SHA-based EVALSHA reuse.
        if self._async_script is None:
            self._async_script = client.register_script(_LUA_TOKEN_BUCKET)
        script = self._async_script
        deadline = time.monotonic() + self.max_wait

        while True:
            result = await script(
                keys=[self.key],
                args=[
                    self.capacity,
                    self.refill_amount,
                    self.refill_interval,
                    time.time(),
                    cost,
                ],
            )
            if result == 1:
                logger.debug("Rate limiter token acquired key=%s", self.key)
                return

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Rate limiter timed out after {self.max_wait}s "
                    f"(key={self.key}). "
                    "Consider increasing DIFY_RPM_CAPACITY or your API quota."
                )

            wait = min(self.retry_interval, remaining)
            logger.debug(
                "Rate limiter waiting %.1fs for token (key=%s remaining=%.1fs)",
                wait,
                self.key,
                remaining,
            )
            await asyncio.sleep(wait)

    async def async_current_tokens(self) -> int:
        """Return current token count (read-only, for monitoring)."""
        client = self._get_async_client()
        val = await client.hget(self.key, "tokens")
        return int(val) if val is not None else self.capacity

    # ── Sync path (Celery workers) ────────────────────────────────────────────

    def _get_sync_client(self) -> sync_redis_lib.Redis:
        if self._sync_client is None:
            self._sync_client = sync_redis_lib.from_url(
                settings.REDIS_URL, decode_responses=True
            )
        return self._sync_client

    def sync_acquire(self, cost: int = 1) -> None:
        """
        Synchronous token acquisition for use inside Celery tasks.

        Raises
        ------
        TimeoutError
            When no token can be acquired within ``max_wait`` seconds.
        """
        client = self._get_sync_client()
        # Cache the registered script on the instance (same rationale as the
        # async path — avoids creating a new Script object on every call).
        if self._sync_script is None:
            self._sync_script = client.register_script(_LUA_TOKEN_BUCKET)
        script = self._sync_script
        deadline = time.monotonic() + self.max_wait

        while True:
            result = script(
                keys=[self.key],
                args=[
                    self.capacity,
                    self.refill_amount,
                    self.refill_interval,
                    time.time(),
                    cost,
                ],
            )
            if result == 1:
                logger.debug("Rate limiter token acquired key=%s", self.key)
                return

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Rate limiter timed out after {self.max_wait}s "
                    f"(key={self.key})."
                )

            wait = min(self.retry_interval, remaining)
            logger.debug(
                "Rate limiter (sync) waiting %.1fs for token key=%s",
                wait,
                self.key,
            )
            time.sleep(wait)

    def sync_current_tokens(self) -> int:
        """Return current token count (sync, read-only, for monitoring)."""
        client = self._get_sync_client()
        val = client.hget(self.key, "tokens")
        return int(val) if val is not None else self.capacity


# ─────────────────────────────────────────────────────────────────────────────
# Global instance — import and use directly
# ─────────────────────────────────────────────────────────────────────────────
#
# Tune via .env:
#   DIFY_RPM_CAPACITY   — burst ceiling (default 60)
#   DIFY_RPM_REFILL     — tokens refilled per interval (default 60)
#   DIFY_RPM_INTERVAL   — refill interval in seconds (default 60)
#
# Example for OpenAI Tier-1 GPT-4o (500 RPM):
#   DIFY_RPM_CAPACITY=100
#   DIFY_RPM_REFILL=500
#   DIFY_RPM_INTERVAL=60
#
dify_rate_limiter = DistributedRateLimiter(
    key="rate_limit:dify",
    capacity=settings.DIFY_RPM_CAPACITY,
    refill_amount=settings.DIFY_RPM_REFILL,
    refill_interval=settings.DIFY_RPM_INTERVAL,
    max_wait=120.0,
    retry_interval=1.0,
)
