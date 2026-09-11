from __future__ import annotations

import os
import uuid

import pytest
from redis.asyncio import Redis

from app.jobs_v22.store import DurableJobStore
from tests.integration.support.redis_process import (
    clear_prefixed_keys_async,
    require_test_redis_environment,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def redis_test_environment() -> tuple[str, str]:
    return require_test_redis_environment(os.environ)


@pytest.fixture
async def redis_client(redis_test_environment: tuple[str, str]):
    redis_url, run_prefix = redis_test_environment
    client = Redis.from_url(redis_url, decode_responses=False)
    await client.ping()
    try:
        yield client
    finally:
        await clear_prefixed_keys_async(client, run_prefix)
        await client.aclose()


@pytest.fixture
def redis_prefix(redis_test_environment: tuple[str, str]) -> str:
    _, run_prefix = redis_test_environment
    return f"{run_prefix}{uuid.uuid4().hex}"


@pytest.fixture
def real_store(redis_client: Redis, redis_prefix: str) -> DurableJobStore:
    return DurableJobStore(redis_client, prefix=redis_prefix, state_ttl_seconds=120)
