import os
import uuid

import pytest
from redis import Redis

from support.network_guard import validate_redis_test_environment


pytestmark = pytest.mark.redis_integration


def test_real_redis_round_trip_uses_isolated_ci_namespace() -> None:
    redis_url = os.environ["V22_TEST_REDIS_URL"]
    prefix = os.environ["V22_TEST_REDIS_PREFIX"]
    validate_redis_test_environment(os.environ)

    client = Redis.from_url(redis_url, decode_responses=True)
    key = f"{prefix}smoke:{uuid.uuid4()}"
    try:
        assert client.ping() is True
        assert client.set(key, "ready", ex=30) is True
        assert client.get(key) == "ready"
    finally:
        client.delete(key)
        client.close()
