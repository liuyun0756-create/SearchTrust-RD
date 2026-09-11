import socket

import pytest
from pytest_socket import SocketBlockedError

from support.network_guard import NetworkGuardError, validate_redis_test_environment


pytestmark = pytest.mark.contract


def test_external_socket_attempt_is_blocked_before_connection() -> None:
    with pytest.warns(UserWarning, match="tried to use socket.socket"), pytest.raises(
        SocketBlockedError
    ):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_test_redis_accepts_only_loopback_nonproduction_database() -> None:
    validate_redis_test_environment(
        {
            "V22_TEST_REDIS_URL": "redis://127.0.0.1:6380/15",
            "V22_TEST_REDIS_PREFIX": "searchtrust:v22:test:0123456789abcdef0123456789abcdef:",
        }
    )


@pytest.mark.parametrize(
    "environment",
    [
        {
            "V22_TEST_REDIS_URL": "rediss://default:secret@redis.railway.internal:6379/0",
            "V22_TEST_REDIS_PREFIX": "searchtrust:v22:test:0123456789abcdef0123456789abcdef:",
        },
        {
            "V22_TEST_REDIS_URL": "redis://127.0.0.1:6379/0",
            "V22_TEST_REDIS_PREFIX": "searchtrust:v22:test:0123456789abcdef0123456789abcdef:",
        },
        {
            "V22_TEST_REDIS_URL": "redis://127.0.0.1:6379/15",
            "V22_TEST_REDIS_PREFIX": "searchtrust:production:",
        },
        {
            "V22_TEST_REDIS_URL": "redis://127.0.0.1:6379/15",
            "V22_TEST_REDIS_PREFIX": "searchtrust:v22:test:0123456789abcdef0123456789abcdef:",
            "RAILWAY_ENVIRONMENT_NAME": "production",
        },
    ],
)
def test_test_redis_rejects_production_like_context(environment: dict[str, str]) -> None:
    with pytest.raises(NetworkGuardError):
        validate_redis_test_environment(environment)
