from app.core.config import Settings


def test_v22_preflight_defaults_are_safe() -> None:
    fields = Settings.model_fields

    assert fields["V22_PREFLIGHT_ENABLED"].default is False
    assert fields["V22_PREFLIGHT_CACHE_TTL_SECONDS"].default == 900
    assert fields["V22_PREFLIGHT_CONNECT_TIMEOUT_SECONDS"].default == 5
    assert fields["V22_PREFLIGHT_READ_TIMEOUT_SECONDS"].default == 10
    assert fields["V22_PREFLIGHT_TOTAL_TIMEOUT_SECONDS"].default == 15
    assert fields["V22_PREFLIGHT_MAX_REDIRECTS"].default == 3
    assert fields["V22_PREFLIGHT_MAX_RESPONSE_BYTES"].default == 2_000_000


def test_pagespeed_key_is_not_exposed_by_settings_repr() -> None:
    settings = Settings(PAGESPEED_API_KEY="pagespeed-secret-value")

    assert "pagespeed-secret-value" not in repr(settings)
