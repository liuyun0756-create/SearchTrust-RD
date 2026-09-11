from __future__ import annotations

import uuid

import pytest
from redis import Redis

from support.network_guard import NetworkGuardError, validate_redis_test_environment
from tests.integration.support.redis_process import clear_prefixed_keys


pytestmark = pytest.mark.redis_integration


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "V22_TEST_REDIS_URL": "redis://127.0.0.1:6379/15",
        "V22_TEST_REDIS_PREFIX": (
            "searchtrust:v22:test:0123456789abcdef0123456789abcdef:"
        ),
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize("key", ["V22_REDIS_URL", "REDIS_URL", "CELERY_BROKER_URL"])
def test_guard_rejects_any_normal_application_redis_variable(key: str) -> None:
    environment = _environment(**{key: "redis://127.0.0.1:6379/15"})

    with pytest.raises(NetworkGuardError, match="application Redis"):
        validate_redis_test_environment(environment)


@pytest.mark.parametrize(
    "url",
    [
        "redis://redis.railway.internal:6379/15",
        "rediss://127.0.0.1:6379/15",
        "redis://127.0.0.1:6379/0",
        "redis://default:secret@127.0.0.1:6379/15",
    ],
)
def test_guard_rejects_urls_outside_test_boundary(url: str) -> None:
    with pytest.raises(NetworkGuardError):
        validate_redis_test_environment(_environment(V22_TEST_REDIS_URL=url))


def test_cleanup_rejects_an_empty_or_handwritten_prefix() -> None:
    class UnusedClient:
        pass

    with pytest.raises(NetworkGuardError, match="generated isolated"):
        clear_prefixed_keys(UnusedClient(), "")  # type: ignore[arg-type]
    with pytest.raises(NetworkGuardError, match="generated isolated"):
        clear_prefixed_keys(  # type: ignore[arg-type]
            UnusedClient(),
            "searchtrust:v22:test:shared:",
        )


def test_prefix_cleanup_cannot_delete_a_sibling_namespace(
    redis_test_environment: tuple[str, str],
) -> None:
    redis_url, prefix = redis_test_environment
    client = Redis.from_url(redis_url, decode_responses=False)
    owned_key = f"{prefix}owned"
    sibling_key = f"searchtrust:v22:test:{uuid.uuid4().hex}:sibling"
    try:
        client.set(owned_key, b"owned")
        client.set(sibling_key, b"sibling")

        assert clear_prefixed_keys(client, prefix) == 1
        assert client.get(owned_key) is None
        assert client.get(sibling_key) == b"sibling"
    finally:
        client.delete(owned_key, sibling_key)
        client.close()
