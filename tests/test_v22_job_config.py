import pytest

from app.core.config import Settings, v22_verified_execution_ready


def test_v22_durable_job_defaults_are_safe() -> None:
    fields = Settings.model_fields

    assert fields["V22_ANALYZE_ENABLED"].default is False
    assert fields["V22_VERIFIED_ANALYSIS_ENABLED"].default is False
    assert fields["V22_JOB_MAX_ATTEMPTS"].default == 3
    assert fields["V22_JOB_STATE_TTL_SECONDS"].default == 7 * 24 * 60 * 60
    assert fields["V22_REDIS_URL"].default == ""
    assert fields["V22_INTERNAL_API_TOKEN"].default == ""
    assert fields["V22_CALLBACK_SECRET"].default == ""
    assert fields["V22_COST_PRICING_REVISION"].default == 1
    assert fields["V22_COST_SERPAPI_REQUEST_USD_MICROS"].default is None
    assert fields["V22_COST_DIFY_INPUT_MTOK_USD_MICROS"].default is None


def test_v22_secrets_are_not_exposed_by_settings_repr() -> None:
    settings = Settings(
        V22_INTERNAL_API_TOKEN="internal-secret-value",
        V22_CALLBACK_SECRET="callback-secret-value",
    )

    rendered = repr(settings)

    assert "internal-secret-value" not in rendered
    assert "callback-secret-value" not in rendered


def test_verified_analysis_flag_is_independent_of_prospect() -> None:
    configured = Settings(_env_file=None, V22_ANALYZE_ENABLED=True)
    assert configured.V22_VERIFIED_ANALYSIS_ENABLED is False
    configured = Settings(_env_file=None, V22_ANALYZE_ENABLED=False, V22_VERIFIED_ANALYSIS_ENABLED=True)
    assert configured.V22_VERIFIED_ANALYSIS_ENABLED is True
    assert configured.V22_ANALYZE_ENABLED is False


@pytest.mark.parametrize(
    ("enabled", "callback_url", "callback_secret", "expected"),
    [
        (False, "https://app.example.com/callback", "secret", False),
        (True, "", "secret", False),
        (True, "https://app.example.com/callback", "", False),
        (True, "https://app.example.com/callback", "secret", True),
    ],
)
def test_verified_execution_readiness_requires_flag_and_complete_callback(
    enabled: bool, callback_url: str, callback_secret: str, expected: bool
) -> None:
    configured = Settings(
        _env_file=None,
        V22_VERIFIED_ANALYSIS_ENABLED=enabled,
        V22_CALLBACK_URL=callback_url,
        V22_CALLBACK_SECRET=callback_secret,
    )
    assert v22_verified_execution_ready(configured) is expected
