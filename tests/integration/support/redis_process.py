"""Create and clean a strictly scoped real-Redis test namespace."""

from __future__ import annotations

from collections.abc import Mapping
import uuid

from redis import Redis
from redis.asyncio import Redis as AsyncRedis

from support.network_guard import (
    validate_generated_redis_test_prefix,
    validate_redis_test_environment,
)


def generated_test_prefix(*, context: str = "test") -> str:
    if context not in {"test", "ci", "local"}:
        raise ValueError("unsupported Redis test prefix context")
    return f"searchtrust:v22:{context}:{uuid.uuid4().hex}:"


def require_test_redis_environment(environment: Mapping[str, str]) -> tuple[str, str]:
    validate_redis_test_environment(environment)
    return environment["V22_TEST_REDIS_URL"], environment["V22_TEST_REDIS_PREFIX"]


def clear_prefixed_keys(client: Redis, prefix: str) -> int:
    """Delete only keys owned by one generated test run."""

    validate_generated_redis_test_prefix(prefix)
    deleted = 0
    cursor: int | str = 0
    while True:
        cursor, keys = client.scan(cursor=cursor, match=f"*{prefix}*", count=200)
        if keys:
            deleted += int(client.delete(*keys))
        if int(cursor) == 0:
            return deleted


async def clear_prefixed_keys_async(client: AsyncRedis, prefix: str) -> int:
    """Async counterpart used by pytest fixtures."""

    validate_generated_redis_test_prefix(prefix)
    deleted = 0
    cursor: int | str = 0
    while True:
        cursor, keys = await client.scan(cursor=cursor, match=f"*{prefix}*", count=200)
        if keys:
            deleted += int(await client.delete(*keys))
        if int(cursor) == 0:
            return deleted
