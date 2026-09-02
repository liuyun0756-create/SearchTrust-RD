"""Checkpointed execution boundary for v2.2 public Findings."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, ValidationError

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.evidence_models import reject_nonfinite
from app.report_v22.findings import build_public_findings
from app.report_v22.findings_errors import FindingsError
from app.report_v22.findings_models import PublicFindingsInput, PublicFindingsResult
from app.report_v22.models import StrictModel


_STAGE_VERSION = "v22_public_findings_stage_v1"
_CHECKPOINT_SCHEMA_VERSION = "public_findings_checkpoint_v1"
_RULESET_VERSION = "v22_public_findings_v1"
logger = logging.getLogger(__name__)

PublicFindingsBuilder = Callable[[PublicFindingsInput], PublicFindingsResult]
PublicFindingsRequest = PublicFindingsInput | dict[str, Any]


class PublicFindingsCheckpointError(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__(
            "V22_PUBLIC_FINDINGS_CHECKPOINT_INVALID",
            "A saved public findings checkpoint could not be validated.",
        )


class PublicFindingsResultError(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__(
            "V22_PUBLIC_FINDINGS_RESULT_INVALID",
            "The public findings stage produced an invalid result.",
        )


class _PublicFindingsCheckpoint(StrictModel):
    schema_version: Literal["public_findings_checkpoint_v1"] = _CHECKPOINT_SCHEMA_VERSION
    job_id: UUID
    input_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    ruleset_version: Literal["v22_public_findings_v1"] = _RULESET_VERSION
    result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    result: PublicFindingsResult


def _validated_request(value: PublicFindingsRequest) -> PublicFindingsInput:
    try:
        reject_nonfinite(value)
        raw = (
            value.model_dump(mode="json", warnings=False)
            if isinstance(value, PublicFindingsInput)
            else value
        )
        return PublicFindingsInput.model_validate_json(canonical_json_bytes(raw))
    except (TypeError, ValidationError, ValueError):
        raise FindingsError("INPUT_INVALID") from None


def _input_digest(value: PublicFindingsInput) -> str:
    return request_digest(
        {
            "stage_version": _STAGE_VERSION,
            "ruleset_version": _RULESET_VERSION,
            "request": value.model_dump(mode="json"),
        }
    )


def _validated_result(value: Any, *, max_bytes: int) -> PublicFindingsResult:
    try:
        reject_nonfinite(value)
        result = PublicFindingsResult.model_validate_json(canonical_json_bytes(value))
    except (TypeError, ValidationError, ValueError):
        raise PublicFindingsResultError() from None
    if result.ruleset_version != _RULESET_VERSION or len(canonical_json_bytes(result)) > max_bytes:
        raise PublicFindingsResultError()
    return result


def _validated_checkpoint(
    value: Any,
    *,
    job_id: UUID,
    input_digest: str,
    max_bytes: int,
) -> _PublicFindingsCheckpoint:
    try:
        checkpoint = _PublicFindingsCheckpoint.model_validate_json(canonical_json_bytes(value))
    except (TypeError, ValidationError, ValueError):
        raise PublicFindingsCheckpointError() from None
    if (
        checkpoint.job_id != job_id
        or checkpoint.input_digest != input_digest
        or checkpoint.ruleset_version != _RULESET_VERSION
        or checkpoint.result.ruleset_version != checkpoint.ruleset_version
        or len(canonical_json_bytes(checkpoint.result)) > max_bytes
        or request_digest(checkpoint.result) != checkpoint.result_checksum
    ):
        raise PublicFindingsCheckpointError()
    return checkpoint


class CheckpointedPublicFindingsStage:
    def __init__(self, *, builder: PublicFindingsBuilder = build_public_findings) -> None:
        self.builder = builder

    def checkpoint_key(self, request: PublicFindingsRequest) -> str:
        digest = _input_digest(_validated_request(request))
        return f"{_STAGE_VERSION}:result:{digest[7:]}"

    async def build(
        self,
        *,
        job_id: UUID,
        request: PublicFindingsRequest,
        checkpoints: JobCheckpoints,
    ) -> PublicFindingsResult:
        validated_request = _validated_request(request)
        input_digest = _input_digest(validated_request)
        key = f"{_STAGE_VERSION}:result:{input_digest[7:]}"
        existing = await checkpoints.get(job_id, key)
        if existing is not None:
            checkpoint = _validated_checkpoint(
                existing,
                job_id=job_id,
                input_digest=input_digest,
                max_bytes=validated_request.limits.max_bytes,
            )
            logger.info(
                "Public findings checkpoint hit job_id=%s digest_suffix=%s findings=%d evaluations=%d",
                job_id,
                input_digest[-12:],
                len(checkpoint.result.findings),
                len(checkpoint.result.rule_evaluations),
            )
            return checkpoint.result

        async def operation() -> dict[str, Any]:
            result = _validated_result(
                self.builder(validated_request),
                max_bytes=validated_request.limits.max_bytes,
            )
            return _PublicFindingsCheckpoint(
                job_id=job_id,
                input_digest=input_digest,
                result_checksum=request_digest(result),
                result=result,
            ).model_dump(mode="json")

        raw = await checkpoints.run_once(job_id, key, operation)
        checkpoint = _validated_checkpoint(
            raw,
            job_id=job_id,
            input_digest=input_digest,
            max_bytes=validated_request.limits.max_bytes,
        )
        logger.info(
            "Public findings checkpoint stored job_id=%s digest_suffix=%s findings=%d evaluations=%d",
            job_id,
            input_digest[-12:],
            len(checkpoint.result.findings),
            len(checkpoint.result.rule_evaluations),
        )
        return checkpoint.result
