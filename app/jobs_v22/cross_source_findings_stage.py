"""Checkpointed execution boundary for V22-071 cross-source Findings."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, ValidationError

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.cross_source_findings import build_cross_source_findings, semantic_first_party_input_checksum
from app.report_v22.cross_source_findings_errors import CrossSourceFindingsError
from app.report_v22.cross_source_findings_models import CrossSourceFindingsInput, CrossSourceFindingsResult
from app.report_v22.models import StrictModel


STAGE_VERSION = "v22_cross_source_findings_stage_v1"
CHECKPOINT_VERSION = "cross_source_findings_checkpoint_v1"
RULESET_VERSION = "v22_cross_source_findings_v1"
logger = logging.getLogger(__name__)

Builder = Callable[[CrossSourceFindingsInput], CrossSourceFindingsResult]
Request = CrossSourceFindingsInput | dict[str, Any]


class CrossSourceFindingsCheckpointError(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__("V22_CROSS_SOURCE_FINDINGS_CHECKPOINT_INVALID",
            "A saved cross-source findings checkpoint could not be validated.")


class CrossSourceFindingsResultError(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__("V22_CROSS_SOURCE_FINDINGS_RESULT_INVALID",
            "The cross-source findings stage produced an invalid result.")


class _Checkpoint(StrictModel):
    schema_version: Literal["cross_source_findings_checkpoint_v1"] = CHECKPOINT_VERSION
    job_id: UUID
    input_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    ruleset_version: Literal["v22_cross_source_findings_v1"] = RULESET_VERSION
    result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    result: CrossSourceFindingsResult


def _request(value: Request) -> CrossSourceFindingsInput:
    try:
        raw = value.model_dump(mode="json", warnings=False) if isinstance(value, CrossSourceFindingsInput) else value
        return CrossSourceFindingsInput.model_validate_json(canonical_json_bytes(raw))
    except (ValidationError, TypeError, ValueError):
        raise CrossSourceFindingsError("INPUT_INVALID") from None


def _digest(value: CrossSourceFindingsInput) -> str:
    payload = value.model_dump(mode="json")
    payload["first_party_input"]["snapshots"] = sorted(
        payload["first_party_input"]["snapshots"], key=lambda item: item["source_type"])
    payload["first_party_input_checksum"] = semantic_first_party_input_checksum(value.first_party_input)
    return request_digest({"stage_version": STAGE_VERSION, "ruleset_version": RULESET_VERSION, "request": payload})


def _result(value: Any, maximum: int) -> CrossSourceFindingsResult:
    try:
        result = CrossSourceFindingsResult.model_validate_json(canonical_json_bytes(value))
    except (ValidationError, TypeError, ValueError):
        raise CrossSourceFindingsResultError() from None
    if result.ruleset_version != RULESET_VERSION or len(canonical_json_bytes(result)) > maximum:
        raise CrossSourceFindingsResultError()
    return result


def _checkpoint(value: Any, *, job_id: UUID, digest: str, maximum: int) -> _Checkpoint:
    try:
        result = _Checkpoint.model_validate_json(canonical_json_bytes(value))
    except (ValidationError, TypeError, ValueError):
        raise CrossSourceFindingsCheckpointError() from None
    if (
        result.job_id != job_id or result.input_digest != digest
        or result.ruleset_version != RULESET_VERSION or result.result.ruleset_version != RULESET_VERSION
        or request_digest(result.result) != result.result_checksum
        or len(canonical_json_bytes(result.result)) > maximum
    ):
        raise CrossSourceFindingsCheckpointError()
    return result


class CheckpointedCrossSourceFindingsStage:
    def __init__(self, *, builder: Builder = build_cross_source_findings) -> None:
        self.builder = builder

    def checkpoint_key(self, request: Request) -> str:
        digest = _digest(_request(request))
        return f"{STAGE_VERSION}:result:{digest[7:]}"

    async def build(self, *, job_id: UUID, request: Request,
                    checkpoints: JobCheckpoints) -> CrossSourceFindingsResult:
        validated = _request(request)
        digest = _digest(validated)
        key = f"{STAGE_VERSION}:result:{digest[7:]}"
        saved = await checkpoints.get(job_id, key)
        if saved is not None:
            return _checkpoint(saved, job_id=job_id, digest=digest, maximum=validated.limits.max_bytes).result

        async def operation() -> dict[str, Any]:
            built = _result(self.builder(validated), validated.limits.max_bytes)
            return _Checkpoint(job_id=job_id, input_digest=digest,
                result_checksum=request_digest(built), result=built).model_dump(mode="json")

        saved = await checkpoints.run_once(job_id, key, operation)
        checked = _checkpoint(saved, job_id=job_id, digest=digest, maximum=validated.limits.max_bytes)
        logger.info("Cross-source findings checkpoint stored job_id=%s digest_suffix=%s findings=%d evaluations=%d",
            job_id, digest[-12:], len(checked.result.findings), len(checked.result.rule_evaluations))
        return checked.result
