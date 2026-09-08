"""Checkpointed execution boundary for V22-070 first-party Findings."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, ValidationError

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.first_party_findings import build_first_party_findings
from app.report_v22.first_party_findings_errors import FirstPartyFindingsError
from app.report_v22.first_party_findings_models import FirstPartyFindingsInput, FirstPartyFindingsResult
from app.report_v22.models import StrictModel


STAGE_VERSION = "v22_first_party_findings_stage_v1"
CHECKPOINT_VERSION = "first_party_findings_checkpoint_v1"
RULESET_VERSION = "v22_first_party_findings_v1"
logger = logging.getLogger(__name__)

FirstPartyFindingsBuilder = Callable[[FirstPartyFindingsInput], FirstPartyFindingsResult]
FirstPartyFindingsRequest = FirstPartyFindingsInput | dict[str, Any]


class FirstPartyFindingsCheckpointError(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__("V22_FIRST_PARTY_FINDINGS_CHECKPOINT_INVALID",
            "A saved first-party findings checkpoint could not be validated.")


class FirstPartyFindingsResultError(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__("V22_FIRST_PARTY_FINDINGS_RESULT_INVALID",
            "The first-party findings stage produced an invalid result.")


class _Checkpoint(StrictModel):
    schema_version: Literal["first_party_findings_checkpoint_v1"] = CHECKPOINT_VERSION
    job_id: UUID
    input_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    ruleset_version: Literal["v22_first_party_findings_v1"] = RULESET_VERSION
    result_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    result: FirstPartyFindingsResult


def _request(value: FirstPartyFindingsRequest) -> FirstPartyFindingsInput:
    try:
        raw = value.model_dump(mode="json", warnings=False) if isinstance(value, FirstPartyFindingsInput) else value
        return FirstPartyFindingsInput.model_validate_json(canonical_json_bytes(raw))
    except (ValidationError, TypeError, ValueError):
        raise FirstPartyFindingsError("INPUT_INVALID") from None


def _digest(value: FirstPartyFindingsInput) -> str:
    return request_digest({
        "stage_version": STAGE_VERSION,
        "ruleset_version": RULESET_VERSION,
        "request": value.model_dump(mode="json"),
    })


def _result(value: Any, maximum: int) -> FirstPartyFindingsResult:
    try:
        result = FirstPartyFindingsResult.model_validate_json(canonical_json_bytes(value))
    except (ValidationError, TypeError, ValueError):
        raise FirstPartyFindingsResultError() from None
    if result.ruleset_version != RULESET_VERSION or len(canonical_json_bytes(result)) > maximum:
        raise FirstPartyFindingsResultError()
    return result


def _checkpoint(value: Any, *, job_id: UUID, digest: str, maximum: int) -> _Checkpoint:
    try:
        result = _Checkpoint.model_validate_json(canonical_json_bytes(value))
    except (ValidationError, TypeError, ValueError):
        raise FirstPartyFindingsCheckpointError() from None
    if (result.job_id != job_id or result.input_digest != digest or result.ruleset_version != RULESET_VERSION
            or result.result.ruleset_version != RULESET_VERSION
            or request_digest(result.result) != result.result_checksum
            or len(canonical_json_bytes(result.result)) > maximum):
        raise FirstPartyFindingsCheckpointError()
    return result


class CheckpointedFirstPartyFindingsStage:
    def __init__(self, *, builder: FirstPartyFindingsBuilder = build_first_party_findings) -> None:
        self.builder = builder

    def checkpoint_key(self, request: FirstPartyFindingsRequest) -> str:
        digest = _digest(_request(request))
        return f"{STAGE_VERSION}:result:{digest[7:]}"

    async def build(self, *, job_id: UUID, request: FirstPartyFindingsRequest,
                    checkpoints: JobCheckpoints) -> FirstPartyFindingsResult:
        validated = _request(request)
        digest = _digest(validated)
        key = f"{STAGE_VERSION}:result:{digest[7:]}"
        saved = await checkpoints.get(job_id, key)
        if saved is not None:
            return _checkpoint(saved, job_id=job_id, digest=digest, maximum=validated.limits.max_bytes).result

        async def operation() -> dict[str, Any]:
            built = _result(self.builder(validated), validated.limits.max_bytes)
            return _Checkpoint(job_id=job_id, input_digest=digest, result_checksum=request_digest(built),
                result=built).model_dump(mode="json")

        saved = await checkpoints.run_once(job_id, key, operation)
        checked = _checkpoint(saved, job_id=job_id, digest=digest, maximum=validated.limits.max_bytes)
        logger.info("First-party findings checkpoint stored job_id=%s digest_suffix=%s findings=%d evaluations=%d",
            job_id, digest[-12:], len(checked.result.findings), len(checked.result.rule_evaluations))
        return checked.result
