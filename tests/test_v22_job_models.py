from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from app.jobs_v22.errors import (
    DeterministicJobError,
    TransientJobError,
    classify_job_exception,
)
from app.jobs_v22.models import JobErrorState, JobState
from app.jobs_v22.cost_models import ALLOWED_COUNTER_KEYS


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
CASE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)


def queued_state(**overrides: object) -> JobState:
    payload: dict[str, object] = {
        "job_id": JOB_ID,
        "case_id": CASE_ID,
        "status": "queued",
        "stage": "queued",
        "progress": 0,
        "message": "Queued",
        "attempt_count": 0,
        "run_generation": 1,
        "revision": 1,
        "request_digest": "sha256:" + "a" * 64,
        "idempotency_key_digest": "sha256:" + "b" * 64,
        "heartbeat_at": None,
        "created_at": NOW,
        "deadline_at": NOW + timedelta(minutes=20),
        "updated_at": NOW,
        "completed_at": None,
        "report": None,
        "error": None,
        "cost_counters": {},
        "callback_synced_revision": 0,
    }
    payload.update(overrides)
    return JobState.model_validate(payload)


def test_job_state_rejects_unknown_fields_and_invalid_progress() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        queued_state(unexpected="nope")

    with pytest.raises(ValidationError):
        queued_state(progress=101)


def test_job_state_accepts_only_complete_bounded_cost_snapshots() -> None:
    counters = {key: 0 for key in ALLOWED_COUNTER_KEYS}
    counters["cost_schema_version"] = 1
    counters["cost_ledger_revision"] = 1
    counters["pricing_revision"] = 1
    assert queued_state(cost_counters=counters).cost_counters == counters

    with pytest.raises(ValidationError):
        queued_state(cost_counters={"provider_attempts_total": 1})
    with pytest.raises(ValidationError):
        queued_state(cost_counters={**counters, "dify_total_tokens": -1})


def test_job_state_requires_structured_terminal_payloads() -> None:
    with pytest.raises(ValidationError, match="failed jobs require"):
        queued_state(status="failed", stage="failed", completed_at=NOW)

    error = JobErrorState(
        error_code="INPUT_INVALID",
        user_message="The request is invalid.",
        retryable=False,
        stage="failed",
        diagnostic_id=UUID("77777777-7777-4777-8777-777777777777"),
    )
    state = queued_state(
        status="failed",
        stage="failed",
        completed_at=NOW,
        error=error,
        revision=2,
    )

    assert state.error == error


def test_error_classification_separates_transient_and_deterministic_failures() -> None:
    request = httpx.Request("GET", "https://provider.example")
    transient = classify_job_exception(httpx.ReadTimeout("timeout", request=request))
    rate_limited = classify_job_exception(
        httpx.HTTPStatusError(
            "limited",
            request=request,
            response=httpx.Response(429, request=request),
        )
    )
    deterministic = classify_job_exception(
        DeterministicJobError("V22_INPUT_INVALID", "The request is invalid.")
    )
    explicit_transient = classify_job_exception(
        TransientJobError("PROVIDER_UNAVAILABLE", "The provider is temporarily unavailable.")
    )

    assert transient.retryable is True
    assert rate_limited.retryable is True
    assert deterministic.retryable is False
    assert explicit_transient.retryable is True
    assert "timeout" not in transient.user_message.casefold()
