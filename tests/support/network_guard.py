from collections.abc import Mapping
from urllib.parse import urlparse


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_TEST_PREFIXES = (
    "searchtrust:v22:test:",
    "searchtrust:v22:ci:",
    "searchtrust:v22:local:",
)
_PRODUCTION_CONTEXT_KEYS = (
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
)


class NetworkGuardError(ValueError):
    """Raised before a test can target production-like infrastructure."""


def validate_redis_test_environment(environment: Mapping[str, str]) -> None:
    for key in _PRODUCTION_CONTEXT_KEYS:
        if environment.get(key):
            raise NetworkGuardError("Railway runtime context is forbidden in Redis tests")
    if environment.get("RAILWAY_ENVIRONMENT_NAME", "").lower() == "production":
        raise NetworkGuardError("production environment is forbidden in Redis tests")

    test_url = environment.get("V22_TEST_REDIS_URL", "")
    parsed = urlparse(test_url)
    if parsed.scheme != "redis" or parsed.hostname not in _LOOPBACK_HOSTS:
        raise NetworkGuardError("Redis tests require a loopback redis:// URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise NetworkGuardError("Redis test URLs cannot contain credentials or options")
    try:
        port = parsed.port
    except ValueError as exc:
        raise NetworkGuardError("Redis test port is invalid") from exc
    if port is None or not 0 < port < 65536:
        raise NetworkGuardError("Redis tests require an explicit valid port")

    database = parsed.path.removeprefix("/")
    if not database.isdigit() or int(database) == 0:
        raise NetworkGuardError("Redis tests require a non-production database")

    prefix = environment.get("V22_TEST_REDIS_PREFIX", "")
    if not prefix.startswith(_TEST_PREFIXES) or not prefix.endswith(":"):
        raise NetworkGuardError("Redis tests require an isolated V2.2 test prefix")

    for key in ("V22_REDIS_URL", "REDIS_URL"):
        configured = environment.get(key)
        if configured and configured != test_url:
            raise NetworkGuardError("non-test Redis configuration is forbidden")
