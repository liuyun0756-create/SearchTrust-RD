from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-quality.yml"

pytestmark = pytest.mark.contract


def _load_workflow() -> dict:
    assert WORKFLOW_PATH.is_file(), "backend quality workflow is required"
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


def _workflow_triggers(workflow: dict) -> dict:
    # PyYAML follows YAML 1.1 and may parse the unquoted GitHub key `on` as True.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    return triggers


def test_backend_quality_runs_for_pull_requests_and_main_pushes() -> None:
    workflow = _load_workflow()
    triggers = _workflow_triggers(workflow)

    assert "pull_request" in triggers
    assert triggers["push"]["branches"] == ["main"]
    assert workflow["concurrency"]["cancel-in-progress"] is True


def test_backend_quality_uses_python_312_pip_cache_and_network_isolation() -> None:
    workflow = _load_workflow()
    quality_job = workflow["jobs"]["backend-quality"]
    steps = quality_job["steps"]

    setup_python = next(
        step for step in steps if step.get("uses", "").startswith("actions/setup-python@")
    )
    assert setup_python["with"]["python-version"] == "3.12"
    assert setup_python["with"]["cache"] == "pip"

    test_step = next(step for step in steps if step.get("name") == "Run backend tests")
    assert "--disable-socket" in test_step["run"]
    assert "--allow-unix-socket" in test_step["run"]
    assert "not redis_integration" in test_step["run"]


def test_redis_integration_job_uses_redis_74_and_localhost_only() -> None:
    workflow = _load_workflow()
    redis_job = workflow["jobs"]["redis-integration"]
    redis_service = redis_job["services"]["redis"]

    assert redis_service["image"] == "redis:7.4-alpine"
    assert "redis-cli ping" in redis_service["options"]

    test_step = next(
        step for step in redis_job["steps"] if step.get("name") == "Run Redis smoke test"
    )
    assert "--disable-socket" in test_step["run"]
    assert "--allow-hosts=127.0.0.1,localhost,::1" in test_step["run"]
    assert redis_job["env"]["V22_TEST_REDIS_URL"] == "redis://127.0.0.1:6379/15"


def test_backend_workflow_does_not_read_production_secrets() -> None:
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "secrets." not in workflow_text
    assert "RAILWAY_" not in workflow_text
    assert "SUPABASE_" not in workflow_text
    assert "SERPAPI_" not in workflow_text
