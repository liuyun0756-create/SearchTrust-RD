"""Strict internal models for durable SearchTrust v2.2 jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.api.v2.models import JobStage
from app.report_v22.models import ReportV22, StrictModel
from app.jobs_v22.cost_models import CostCountersV1


JobStatus = Literal["queued", "running", "succeeded", "failed"]


def _validated_cost_counters(value: dict[str, int | float]) -> dict[str, int]:
    if not value:
        return {}
    return CostCountersV1.model_validate(value).root


class JobErrorState(StrictModel):
    """Safe terminal failure details persisted in Redis and exposed publicly."""

    error_code: str = Field(min_length=1, max_length=120, pattern=r"^[A-Z0-9_]+$")
    user_message: str = Field(min_length=1, max_length=500)
    retryable: bool
    stage: JobStage
    diagnostic_id: UUID


class JobState(StrictModel):
    """Latest recoverable snapshot for one logical v2.2 task."""

    job_id: UUID
    case_id: UUID
    status: JobStatus
    stage: JobStage
    progress: int = Field(ge=0, le=100)
    message: str = Field(min_length=1, max_length=500)
    attempt_count: int = Field(ge=0)
    run_generation: int = Field(ge=1)
    revision: int = Field(ge=1)
    request_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    idempotency_key_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    heartbeat_at: AwareDatetime | None = None
    created_at: AwareDatetime
    deadline_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    report: ReportV22 | None = None
    error: JobErrorState | None = None
    cost_counters: dict[str, int | float] = Field(default_factory=dict)
    callback_synced_revision: int = Field(default=0, ge=0)

    @field_validator("cost_counters")
    @classmethod
    def validate_cost_counters(cls, value):
        return _validated_cost_counters(value)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "JobState":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.deadline_at <= self.created_at:
            raise ValueError("deadline_at must follow created_at")
        if self.callback_synced_revision > self.revision:
            raise ValueError("callback synced revision cannot exceed state revision")
        if self.status == "succeeded":
            if (
                self.report is None
                or self.error is not None
                or self.stage != "completed"
                or self.progress != 100
                or self.completed_at is None
            ):
                raise ValueError("succeeded jobs require a completed report and no error")
        elif self.status == "failed":
            if self.error is None or self.report is not None or self.stage != "failed" or self.completed_at is None:
                raise ValueError("failed jobs require an error and no report")
        elif self.report is not None or self.error is not None or self.completed_at is not None:
            raise ValueError("non-terminal jobs must not contain terminal payloads")
        return self

    @property
    def terminal(self) -> bool:
        return self.status in {"succeeded", "failed"}


class JobCallbackEvent(StrictModel):
    """Bounded state event sent to the Next.js audit owner."""

    event_id: str = Field(min_length=1, max_length=200)
    job_id: UUID
    case_id: UUID
    revision: int = Field(ge=1)
    status: JobStatus
    stage: JobStage
    progress: int = Field(ge=0, le=100)
    message: str = Field(min_length=1, max_length=500)
    attempt_count: int = Field(ge=0)
    run_generation: int = Field(ge=1)
    deadline_at: AwareDatetime
    heartbeat_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    error: JobErrorState | None = None
    cost_counters: dict[str, int | float] = Field(default_factory=dict)
    occurred_at: AwareDatetime

    @field_validator("cost_counters")
    @classmethod
    def validate_cost_counters(cls, value):
        return _validated_cost_counters(value)


class QueueHealth(StrictModel):
    status: Literal["ok", "degraded"]
    redis_connected: bool
    worker_alive: bool
    pending_callbacks: int = Field(ge=0)
    checked_at: AwareDatetime


def utc_now() -> datetime:
    """Single timezone-aware clock helper that tests can replace."""

    from datetime import timezone

    return datetime.now(timezone.utc)
