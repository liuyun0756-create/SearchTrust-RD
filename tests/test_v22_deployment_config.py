from pathlib import Path

import tomllib
import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_compose_defines_persistent_redis_web_and_arq_worker() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert {"app", "worker", "redis"}.issubset(services)
    redis_command = " ".join(services["redis"]["command"])
    assert "appendonly yes" in redis_command
    assert services["redis"]["volumes"] == ["redis_data:/data"]
    assert "arq app.jobs_v22.worker.WorkerSettings" in services["worker"]["command"]
    assert services["app"]["environment"]["V22_REDIS_URL"] == "redis://redis:6379/0"
    assert services["worker"]["environment"]["V22_REDIS_URL"] == "redis://redis:6379/0"


def test_railway_worker_runs_arq_not_uvicorn() -> None:
    config = tomllib.loads((ROOT / "railway.worker.toml").read_text(encoding="utf-8"))
    command = config["deploy"]["startCommand"]

    assert command == "arq app.jobs_v22.worker.WorkerSettings"
    assert "uvicorn" not in command


def test_environment_template_is_disabled_and_contains_no_real_secrets() -> None:
    template = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "V22_ANALYZE_ENABLED=false" in template
    assert "V22_PREFLIGHT_ENABLED=false" in template
    assert "V22_PREFLIGHT_CACHE_TTL_SECONDS=900" in template
    assert "V22_PREFLIGHT_MAX_RESPONSE_BYTES=2000000" in template
    assert "V22_SITE_INVENTORY_CONCURRENCY=10" in template
    assert "V22_SITE_INVENTORY_STRUCTURAL_BYTES=256000" in template
    assert "V22_SITE_INVENTORY_DEEP_BYTES=2000000" in template
    assert "V22_SERP_MARKET_TOTAL_TIMEOUT_SECONDS=45" in template
    assert "V22_SERP_MARKET_MAX_RESPONSE_BYTES=2000000" in template
    assert "V22_COMPETITOR_DISCOVERY_ENABLED=false" in template
    assert "V22_COMPETITOR_MARKET_TTL_SECONDS=86400" in template
    assert "V22_COMPETITOR_STATE_TTL_SECONDS=604800" in template
    assert "V22_COMPETITOR_TOTAL_TIMEOUT_SECONDS=45" in template
    assert "V22_COMPETITOR_MAX_RESPONSE_BYTES=2000000" in template
    assert "SERPAPI_LOCATIONS_URL=https://serpapi.com/locations.json" in template
    assert "PAGESPEED_API_KEY=" in template
    assert "V22_REDIS_URL=redis://localhost:6379/0" in template
    assert "V22_INTERNAL_API_TOKEN=replace-with-a-long-random-value" in template
    assert "V22_CALLBACK_SECRET=replace-with-a-different-long-random-value" in template
    assert "V22_SUPABASE_SERVICE_ROLE_KEY=your-service-role-key" in template
    assert "eyJ" not in next(
        line for line in template.splitlines()
        if line.startswith("V22_SUPABASE_SERVICE_ROLE_KEY=")
    )
