"""Checkpointed execution boundary for V22-073 version differences."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, ValidationError

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.models import StrictModel
from app.report_v22.version_diff import build_version_diff
from app.report_v22.version_diff_errors import VersionDiffError
from app.report_v22.version_diff_models import VersionDiffBuildInput, VersionDiffBuildResult


STAGE_VERSION = "v22_version_diff_stage_v1"
CHECKPOINT_VERSION = "version_diff_checkpoint_v1"
RULESET_VERSION = "v22_version_diff_v1"
REASON_CATALOG_VERSION = "v22_version_diff_reasons_v1"
logger = logging.getLogger(__name__)

Builder = Callable[[VersionDiffBuildInput], VersionDiffBuildResult]
Request = VersionDiffBuildInput | dict[str, Any]


class VersionDiffCheckpointError(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__(
            "V22_VERSION_DIFF_CHECKPOINT_INVALID",
            "A saved version difference checkpoint could not be validated.",
        )


class VersionDiffResultError(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__(
            "V22_VERSION_DIFF_RESULT_INVALID",
            "The version difference stage produced an invalid result.",
        )


class _Checkpoint(StrictModel):
    schema_version: Literal["version_diff_checkpoint_v1"] = CHECKPOINT_VERSION
    job_id: UUID
    input_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    ruleset_version: Literal["v22_version_diff_v1"] = RULESET_VERSION
    reason_catalog_version: Literal["v22_version_diff_reasons_v1"] = REASON_CATALOG_VERSION
    result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    result: VersionDiffBuildResult


def _request(value: Request) -> VersionDiffBuildInput:
    try:
        raw = value.model_dump(mode="json", warnings=False) if isinstance(value, VersionDiffBuildInput) else value
        checked = VersionDiffBuildInput.model_validate_json(canonical_json_bytes(raw))
    except (ValidationError, TypeError, ValueError):
        raise VersionDiffError("INPUT_INVALID") from None
    if (
        request_digest(checked.parent_report) != checked.parent_report_checksum
        or request_digest(checked.verified_reprioritization_input)
        != checked.verified_reprioritization_input_checksum
        or request_digest(checked.verified_reprioritization_result)
        != checked.verified_reprioritization_result_checksum
    ):
        raise VersionDiffError("CHECKSUM_MISMATCH")
    return checked


def _digest(value: VersionDiffBuildInput) -> str:
    return request_digest({
        "stage_version": STAGE_VERSION,
        "ruleset_version": RULESET_VERSION,
        "reason_catalog_version": REASON_CATALOG_VERSION,
        "case_id": str(value.case_id),
        "parent_report_id": value.parent_report_id,
        "evaluated_at": value.evaluated_at.isoformat(),
        "parent_report_checksum": value.parent_report_checksum,
        "verified_reprioritization_input_checksum": value.verified_reprioritization_input_checksum,
        "verified_reprioritization_result_checksum": value.verified_reprioritization_result_checksum,
        "limits": value.limits.model_dump(mode="json"),
    })


def _result(value: Any, maximum: int) -> VersionDiffBuildResult:
    try:
        result = VersionDiffBuildResult.model_validate_json(canonical_json_bytes(value))
    except (ValidationError, TypeError, ValueError):
        raise VersionDiffResultError() from None
    if (
        result.ruleset_version != RULESET_VERSION
        or result.reason_catalog_version != REASON_CATALOG_VERSION
        or len(canonical_json_bytes(result)) > maximum
    ):
        raise VersionDiffResultError()
    return result


def _checkpoint(value: Any, *, job_id: UUID, digest: str, maximum: int) -> _Checkpoint:
    try:
        checked = _Checkpoint.model_validate_json(canonical_json_bytes(value))
    except (ValidationError, TypeError, ValueError):
        raise VersionDiffCheckpointError() from None
    if (
        checked.job_id != job_id
        or checked.input_digest != digest
        or checked.ruleset_version != RULESET_VERSION
        or checked.reason_catalog_version != REASON_CATALOG_VERSION
        or request_digest(checked.result) != checked.result_checksum
        or len(canonical_json_bytes(checked.result)) > maximum
    ):
        raise VersionDiffCheckpointError()
    return checked


class CheckpointedVersionDiffStage:
    def __init__(self, *, builder: Builder = build_version_diff) -> None:
        self.builder = builder

    def checkpoint_key(self, request: Request) -> str:
        digest = _digest(_request(request))
        return f"{STAGE_VERSION}:result:{digest[7:]}"

    async def build(
        self, *, job_id: UUID, request: Request, checkpoints: JobCheckpoints,
    ) -> VersionDiffBuildResult:
        validated = _request(request)
        digest = _digest(validated)
        key = f"{STAGE_VERSION}:result:{digest[7:]}"
        saved = await checkpoints.get(job_id, key)
        if saved is not None:
            return _checkpoint(
                saved, job_id=job_id, digest=digest, maximum=validated.limits.max_bytes,
            ).result

        async def operation() -> dict[str, Any]:
            built = _result(self.builder(validated), validated.limits.max_bytes)
            return _Checkpoint(
                job_id=job_id,
                input_digest=digest,
                result_checksum=request_digest(built),
                result=built,
            ).model_dump(mode="json")

        saved = await checkpoints.run_once(job_id, key, operation)
        checked = _checkpoint(
            saved, job_id=job_id, digest=digest, maximum=validated.limits.max_bytes,
        )
        logger.info(
            "Version difference checkpoint stored job_id=%s digest_suffix=%s entries=%d",
            job_id,
            digest[-12:],
            len(checked.result.version_diff.entries),
        )
        return checked.result
