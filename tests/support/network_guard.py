from collections.abc import Mapping
import ipaddress
import re
from urllib.parse import urlparse


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_TEST_PREFIX_PATTERN = re.compile(
    r"^searchtrust:v22:(?:test|ci|local):[0-9a-f]{32}:$"
)
_APPLICATION_REDIS_KEYS = (
    "V22_REDIS_URL",
    "REDIS_URL",
    "CACHE_REDIS_URL",
    "CELERY_BROKER_URL",
    "RQ_REDIS_URL",
)
_PRODUCTION_CONTEXT_KEYS = (
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
)


class NetworkGuardError(ValueError):
    """Raised before a test can target production-like infrastructure."""


def validate_generated_redis_test_prefix(prefix: str) -> None:
    if not _TEST_PREFIX_PATTERN.fullmatch(prefix):
        raise NetworkGuardError("Redis tests require a generated isolated V2.2 test prefix")


def validate_redis_test_environment(environment: Mapping[str, str]) -> None:
    for key in _PRODUCTION_CONTEXT_KEYS:
        if environment.get(key):
            raise NetworkGuardError("Railway runtime context is forbidden in Redis tests")
    if environment.get("RAILWAY_ENVIRONMENT_NAME", "").lower() == "production":
        raise NetworkGuardError("production environment is forbidden in Redis tests")

    test_url = environment.get("V22_TEST_REDIS_URL", "")
    parsed = urlparse(test_url)
    hostname = (parsed.hostname or "").lower()
    github_service = hostname == "redis" and environment.get("GITHUB_ACTIONS") == "true"
    if parsed.scheme != "redis" or (hostname not in _LOOPBACK_HOSTS and not github_service):
        raise NetworkGuardError("Redis tests require an approved test-only redis:// URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise NetworkGuardError("Redis test URLs cannot contain credentials or options")
    try:
        port = parsed.port
    except ValueError as exc:
        raise NetworkGuardError("Redis test port is invalid") from exc
    if port is None or not 0 < port < 65536:
        raise NetworkGuardError("Redis tests require an explicit valid port")

    if hostname != "localhost" and hostname in _LOOPBACK_HOSTS:
        if not ipaddress.ip_address(hostname).is_loopback:
            raise NetworkGuardError("Redis test host resolved outside loopback")

    database = parsed.path.removeprefix("/")
    if not database.isdigit() or int(database) == 0:
        raise NetworkGuardError("Redis tests require a non-production database")

    prefix = environment.get("V22_TEST_REDIS_PREFIX", "")
    validate_generated_redis_test_prefix(prefix)

    for key in _APPLICATION_REDIS_KEYS:
        if environment.get(key):
            raise NetworkGuardError("application Redis configuration is forbidden in tests")
