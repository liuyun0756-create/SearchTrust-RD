"""Checkpointed execution boundary for V22-072 verified reprioritization."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, ValidationError

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.actions import canonical_public_findings
from app.report_v22.cross_source_findings import semantic_first_party_input_checksum
from app.report_v22.models import StrictModel
from app.report_v22.verified_reprioritization import build_verified_reprioritization
from app.report_v22.verified_reprioritization_errors import VerifiedReprioritizationError
from app.report_v22.verified_reprioritization_models import (
    VerifiedReprioritizationInput, VerifiedReprioritizationResult,
)


STAGE_VERSION = "v22_verified_reprioritization_stage_v1"
CHECKPOINT_VERSION = "verified_reprioritization_checkpoint_v1"
RULESET_VERSION = "v22_verified_reprioritization_v1"
CATALOG_VERSION = "v22_public_actions_v1"
logger = logging.getLogger(__name__)

Builder = Callable[[VerifiedReprioritizationInput], VerifiedReprioritizationResult]
Request = VerifiedReprioritizationInput | dict[str, Any]


class VerifiedReprioritizationCheckpointError(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__(
            "V22_VERIFIED_REPRIORITIZATION_CHECKPOINT_INVALID",
            "A saved verified reprioritization checkpoint could not be validated.",
        )


class VerifiedReprioritizationResultError(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__(
            "V22_VERIFIED_REPRIORITIZATION_RESULT_INVALID",
            "The verified reprioritization stage produced an invalid result.",
        )


class _Checkpoint(StrictModel):
    schema_version: Literal["verified_reprioritization_checkpoint_v1"] = CHECKPOINT_VERSION
    job_id: UUID
    input_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    ruleset_version: Literal["v22_verified_reprioritization_v1"] = RULESET_VERSION
    action_catalog_version: Literal["v22_public_actions_v1"] = CATALOG_VERSION
    result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    result: VerifiedReprioritizationResult


def _request(value: Request) -> VerifiedReprioritizationInput:
    try:
        raw = value.model_dump(mode="json", warnings=False) if isinstance(value, VerifiedReprioritizationInput) else value
        return VerifiedReprioritizationInput.model_validate_json(canonical_json_bytes(raw))
    except (ValidationError, TypeError, ValueError):
        raise VerifiedReprioritizationError("INPUT_INVALID") from None


def _digest(value: VerifiedReprioritizationInput) -> str:
    payload = value.model_dump(mode="json")
    public_input = payload["public_findings_input"]["evidence_input"]
    public_input["sources"] = sorted(public_input["sources"], key=canonical_json_bytes)
    public_input["missing_sources"] = sorted(public_input["missing_sources"], key=canonical_json_bytes)
    payload["public_findings_input_checksum"] = request_digest(payload["public_findings_input"])
    payload["public_findings_result"] = canonical_public_findings(value.public_findings_result).model_dump(mode="json")
    payload["first_party_input"]["snapshots"] = sorted(
        payload["first_party_input"]["snapshots"], key=lambda item: item["source_type"],
    )
    payload["first_party_input_checksum"] = semantic_first_party_input_checksum(value.first_party_input)
    return request_digest({
        "stage_version": STAGE_VERSION,
        "ruleset_version": RULESET_VERSION,
        "action_catalog_version": CATALOG_VERSION,
        "request": payload,
    })


def _result(value: Any, maximum: int) -> VerifiedReprioritizationResult:
    try:
        result = VerifiedReprioritizationResult.model_validate_json(canonical_json_bytes(value))
    except (ValidationError, TypeError, ValueError):
        raise VerifiedReprioritizationResultError() from None
    if result.ruleset_version != RULESET_VERSION or len(canonical_json_bytes(result)) > maximum:
        raise VerifiedReprioritizationResultError()
    return result


def _checkpoint(value: Any, *, job_id: UUID, digest: str, maximum: int) -> _Checkpoint:
    try:
        checked = _Checkpoint.model_validate_json(canonical_json_bytes(value))
    except (ValidationError, TypeError, ValueError):
        raise VerifiedReprioritizationCheckpointError() from None
    if (
        checked.job_id != job_id
        or checked.input_digest != digest
        or checked.ruleset_version != RULESET_VERSION
        or checked.action_catalog_version != CATALOG_VERSION
        or request_digest(checked.result) != checked.result_checksum
        or len(canonical_json_bytes(checked.result)) > maximum
    ):
        raise VerifiedReprioritizationCheckpointError()
    return checked


class CheckpointedVerifiedReprioritizationStage:
    def __init__(self, *, builder: Builder = build_verified_reprioritization) -> None:
        self.builder = builder

    def checkpoint_key(self, request: Request) -> str:
        digest = _digest(_request(request))
        return f"{STAGE_VERSION}:result:{digest[7:]}"

    async def build(
        self, *, job_id: UUID, request: Request, checkpoints: JobCheckpoints,
    ) -> VerifiedReprioritizationResult:
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
            "Verified reprioritization checkpoint stored job_id=%s digest_suffix=%s actions=%d relations=%d",
            job_id, digest[-12:], len(checked.result.actions), len(checked.result.relations),
        )
        return checked.result
