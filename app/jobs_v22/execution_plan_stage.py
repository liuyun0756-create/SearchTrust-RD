"""Checkpointed execution boundary for V22-074 verified execution plans."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, ValidationError

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.execution_plan import build_execution_plan, validate_execution_plan_privacy
from app.report_v22.execution_plan_catalog import (
    COPY_CATALOG_VERSION, METRIC_CATALOG_VERSION, RULESET_VERSION,
)
from app.report_v22.execution_plan_errors import ExecutionPlanError
from app.report_v22.execution_plan_models import ExecutionPlanBuildInput, ExecutionPlanBuildResult
from app.report_v22.models import StrictModel


STAGE_VERSION = "v22_execution_plan_stage_v1"
CHECKPOINT_VERSION = "execution_plan_checkpoint_v1"
logger = logging.getLogger(__name__)

Builder = Callable[[ExecutionPlanBuildInput], ExecutionPlanBuildResult]
Request = ExecutionPlanBuildInput | dict[str, Any]


class ExecutionPlanCheckpointError(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__(
            "V22_EXECUTION_PLAN_CHECKPOINT_INVALID",
            "A saved verified execution plan checkpoint could not be validated.",
        )


class ExecutionPlanResultError(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__(
            "V22_EXECUTION_PLAN_RESULT_INVALID",
            "The verified execution plan stage produced an invalid result.",
        )


class _Checkpoint(StrictModel):
    schema_version: Literal["execution_plan_checkpoint_v1"] = CHECKPOINT_VERSION
    job_id: UUID
    input_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    ruleset_version: Literal["v22_execution_plan_v1"] = RULESET_VERSION
    metric_catalog_version: Literal["v22_execution_metrics_v1"] = METRIC_CATALOG_VERSION
    copy_catalog_version: Literal["v22_verified_copy_v1"] = COPY_CATALOG_VERSION
    result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    result: ExecutionPlanBuildResult


def _request(value: Request) -> ExecutionPlanBuildInput:
    try:
        raw = value.model_dump(mode="json", warnings=False) if isinstance(value, ExecutionPlanBuildInput) else value
        checked = ExecutionPlanBuildInput.model_validate_json(canonical_json_bytes(raw))
    except (ValidationError, TypeError, ValueError):
        raise ExecutionPlanError("INPUT_INVALID") from None
    pairs = (
        (checked.verified_reprioritization_input, checked.verified_reprioritization_input_checksum),
        (checked.verified_reprioritization_result, checked.verified_reprioritization_result_checksum),
        (checked.version_diff_input, checked.version_diff_input_checksum),
        (checked.version_diff_result, checked.version_diff_result_checksum),
    )
    if any(request_digest(item) != checksum for item, checksum in pairs):
        raise ExecutionPlanError("CHECKSUM_MISMATCH")
    return checked


def _digest(value: ExecutionPlanBuildInput) -> str:
    return request_digest({
        "stage_version": STAGE_VERSION,
        "ruleset_version": RULESET_VERSION,
        "metric_catalog_version": METRIC_CATALOG_VERSION,
        "copy_catalog_version": COPY_CATALOG_VERSION,
        "case_id": value.case_id,
        "parent_report_id": value.parent_report_id,
        "verified_report_id": value.verified_report_id,
        "evaluated_at": value.evaluated_at.isoformat(),
        "planning_date": value.planning_date.isoformat(),
        "verified_reprioritization_input_checksum": value.verified_reprioritization_input_checksum,
        "verified_reprioritization_result_checksum": value.verified_reprioritization_result_checksum,
        "version_diff_input_checksum": value.version_diff_input_checksum,
        "version_diff_result_checksum": value.version_diff_result_checksum,
        "copy_model_version": value.copy_model_version,
        "limits": value.limits.model_dump(mode="json"),
    })


def _result(value: Any, maximum: int) -> ExecutionPlanBuildResult:
    try:
        checked = ExecutionPlanBuildResult.model_validate_json(canonical_json_bytes(value))
    except (ValidationError, TypeError, ValueError):
        raise ExecutionPlanResultError() from None
    if (
        checked.ruleset_version != RULESET_VERSION
        or checked.metric_catalog_version != METRIC_CATALOG_VERSION
        or checked.copy_catalog_version != COPY_CATALOG_VERSION
        or len(canonical_json_bytes(checked)) > maximum
    ):
        raise ExecutionPlanResultError()
    return checked


def _checkpoint(value: Any, *, job_id: UUID, digest: str, maximum: int) -> _Checkpoint:
    try:
        checked = _Checkpoint.model_validate_json(canonical_json_bytes(value))
    except (ValidationError, TypeError, ValueError):
        raise ExecutionPlanCheckpointError() from None
    if (
        checked.job_id != job_id
        or checked.input_digest != digest
        or checked.ruleset_version != RULESET_VERSION
        or checked.metric_catalog_version != METRIC_CATALOG_VERSION
        or checked.copy_catalog_version != COPY_CATALOG_VERSION
        or request_digest(checked.result) != checked.result_checksum
        or len(canonical_json_bytes(checked.result)) > maximum
    ):
        raise ExecutionPlanCheckpointError()
    return checked


class CheckpointedExecutionPlanStage:
    def __init__(self, *, builder: Builder = build_execution_plan) -> None:
        self.builder = builder

    def checkpoint_key(self, request: Request) -> str:
        digest = _digest(_request(request))
        return f"{STAGE_VERSION}:result:{digest[7:]}"

    async def build(
        self, *, job_id: UUID, request: Request, checkpoints: JobCheckpoints,
    ) -> ExecutionPlanBuildResult:
        validated = _request(request)
        digest = _digest(validated)
        key = f"{STAGE_VERSION}:result:{digest[7:]}"
        saved = await checkpoints.get(job_id, key)
        if saved is not None:
            result = _checkpoint(
                saved, job_id=job_id, digest=digest, maximum=validated.limits.max_bytes,
            ).result
            validate_execution_plan_privacy(validated, result)
            return result

        async def operation() -> dict[str, Any]:
            built = _result(self.builder(validated), validated.limits.max_bytes)
            return _Checkpoint(
                job_id=job_id, input_digest=digest,
                result_checksum=request_digest(built), result=built,
            ).model_dump(mode="json")

        saved = await checkpoints.run_once(job_id, key, operation)
        checked = _checkpoint(
            saved, job_id=job_id, digest=digest, maximum=validated.limits.max_bytes,
        )
        validate_execution_plan_privacy(validated, checked.result)
        logger.info(
            "Execution plan checkpoint stored job_id=%s digest_suffix=%s actions=%d",
            job_id, digest[-12:], len(checked.result.actions),
        )
        return checked.result
