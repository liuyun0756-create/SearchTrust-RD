from app.core.config import Settings


def test_v22_durable_job_defaults_are_safe() -> None:
    fields = Settings.model_fields

    assert fields["V22_ANALYZE_ENABLED"].default is False
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
