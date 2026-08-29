import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_competitor_defaults_are_safe_and_bounded() -> None:
    fields = Settings.model_fields

    assert fields["V22_COMPETITOR_DISCOVERY_ENABLED"].default is False
    assert fields["V22_COMPETITOR_MARKET_TTL_SECONDS"].default == 86_400
    assert fields["V22_COMPETITOR_STATE_TTL_SECONDS"].default == 604_800
    assert fields["V22_COMPETITOR_CONNECT_TIMEOUT_SECONDS"].default == 5
    assert fields["V22_COMPETITOR_READ_TIMEOUT_SECONDS"].default == 30
    assert fields["V22_COMPETITOR_TOTAL_TIMEOUT_SECONDS"].default == 45
    assert fields["V22_COMPETITOR_MAX_RESPONSE_BYTES"].default == 2_000_000


def test_competitor_network_and_ttl_limits_cannot_expand() -> None:
    with pytest.raises(ValidationError):
        Settings(V22_COMPETITOR_MARKET_TTL_SECONDS=86_401)
    with pytest.raises(ValidationError):
        Settings(V22_COMPETITOR_STATE_TTL_SECONDS=604_801)
    with pytest.raises(ValidationError):
        Settings(V22_COMPETITOR_CONNECT_TIMEOUT_SECONDS=31)
    with pytest.raises(ValidationError):
        Settings(V22_COMPETITOR_READ_TIMEOUT_SECONDS=61)
    with pytest.raises(ValidationError):
        Settings(V22_COMPETITOR_TOTAL_TIMEOUT_SECONDS=121)
    with pytest.raises(ValidationError):
        Settings(V22_COMPETITOR_MAX_RESPONSE_BYTES=5_000_001)


def test_competitor_configuration_does_not_enable_analysis_or_discovery() -> None:
    configured = Settings(V22_COMPETITOR_TOTAL_TIMEOUT_SECONDS=60)

    assert configured.V22_ANALYZE_ENABLED is False
    assert configured.V22_COMPETITOR_DISCOVERY_ENABLED is False
