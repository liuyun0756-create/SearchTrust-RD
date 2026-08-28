from pydantic import ValidationError
import pytest

from app.core.config import Settings


def test_site_inventory_defaults_are_bounded() -> None:
    fields = Settings.model_fields

    assert fields["V22_SITE_INVENTORY_CONCURRENCY"].default == 10
    assert fields["V22_SITE_INVENTORY_REQUESTS_PER_SECOND"].default == 5
    assert fields["V22_SITE_INVENTORY_CONNECT_TIMEOUT_SECONDS"].default == 5
    assert fields["V22_SITE_INVENTORY_READ_TIMEOUT_SECONDS"].default == 15
    assert fields["V22_SITE_INVENTORY_TOTAL_TIMEOUT_SECONDS"].default == 30
    assert fields["V22_SITE_INVENTORY_MAX_REDIRECTS"].default == 5
    assert fields["V22_SITE_INVENTORY_STRUCTURAL_BYTES"].default == 256_000
    assert fields["V22_SITE_INVENTORY_DEEP_BYTES"].default == 2_000_000
    assert fields["V22_SITE_INVENTORY_SITEMAP_BYTES"].default == 2_000_000
    assert fields["V22_SITE_INVENTORY_SITEMAP_DECOMPRESSED_BYTES"].default == 10_000_000
    assert fields["V22_SITE_INVENTORY_SITEMAP_INDEX_DEPTH"].default == 2
    assert fields["V22_SITE_INVENTORY_SITEMAP_FILES"].default == 20
    assert fields["V22_SITE_INVENTORY_BATCH_SIZE"].default == 25
    assert fields["V22_SITE_INVENTORY_FIRECRAWL_ENABLED"].default is True


def test_site_inventory_limits_cannot_exceed_safety_caps() -> None:
    with pytest.raises(ValidationError):
        Settings(V22_SITE_INVENTORY_CONCURRENCY=21)
    with pytest.raises(ValidationError):
        Settings(V22_SITE_INVENTORY_STRUCTURAL_BYTES=1_000_001)
    with pytest.raises(ValidationError):
        Settings(V22_SITE_INVENTORY_DEEP_BYTES=5_000_001)


def test_site_inventory_configuration_does_not_enable_analysis() -> None:
    configured = Settings(V22_SITE_INVENTORY_FIRECRAWL_ENABLED=True)

    assert configured.V22_ANALYZE_ENABLED is False
