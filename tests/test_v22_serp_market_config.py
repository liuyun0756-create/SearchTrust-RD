from pydantic import ValidationError
import pytest

from app.collectors.serp_market_models import (
    SERP_CALLS_PER_QUERY,
    SERP_LOGICAL_CALL_LIMIT,
    SERP_MAP_ZOOM,
    SERP_PROVIDER_ATTEMPT_LIMIT_PER_CALL,
    SERP_QUERY_MAX,
    SERP_QUERY_MIN,
    SERP_RESULT_LIMIT_PER_TYPE,
)
from app.core.config import Settings


def test_serp_market_defaults_and_hard_limits_are_bounded() -> None:
    fields = Settings.model_fields

    assert fields["SERPAPI_LOCATIONS_URL"].default == "https://serpapi.com/locations.json"
    assert fields["V22_SERP_MARKET_CONNECT_TIMEOUT_SECONDS"].default == 5
    assert fields["V22_SERP_MARKET_READ_TIMEOUT_SECONDS"].default == 30
    assert fields["V22_SERP_MARKET_TOTAL_TIMEOUT_SECONDS"].default == 45
    assert fields["V22_SERP_MARKET_MAX_RESPONSE_BYTES"].default == 2_000_000
    assert (
        SERP_QUERY_MIN,
        SERP_QUERY_MAX,
        SERP_CALLS_PER_QUERY,
        SERP_LOGICAL_CALL_LIMIT,
        SERP_PROVIDER_ATTEMPT_LIMIT_PER_CALL,
        SERP_RESULT_LIMIT_PER_TYPE,
        SERP_MAP_ZOOM,
    ) == (3, 5, 2, 10, 3, 20, 14)


def test_serp_market_network_limits_cannot_exceed_safety_caps() -> None:
    with pytest.raises(ValidationError):
        Settings(V22_SERP_MARKET_CONNECT_TIMEOUT_SECONDS=31)
    with pytest.raises(ValidationError):
        Settings(V22_SERP_MARKET_READ_TIMEOUT_SECONDS=61)
    with pytest.raises(ValidationError):
        Settings(V22_SERP_MARKET_TOTAL_TIMEOUT_SECONDS=121)
    with pytest.raises(ValidationError):
        Settings(V22_SERP_MARKET_MAX_RESPONSE_BYTES=5_000_001)


def test_serp_market_configuration_does_not_enable_analysis() -> None:
    configured = Settings(V22_SERP_MARKET_TOTAL_TIMEOUT_SECONDS=60)

    assert configured.V22_ANALYZE_ENABLED is False

