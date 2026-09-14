"""Exact routing and execution boundaries for durable Verified jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError

from app.competitors_v22.selection import AnalysisRequestEnvelope
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.cost_ledger import JobCostLedger
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError
from app.jobs_v22.verified_input_resolver import (
    TrustedVerifiedInput,
    require_trusted_verified_input,
)
from app.jobs_v22.verified_models import VerifiedRequestEnvelope, VerifiedTaskRequest
from app.report_v22.models import ReportV22


class VerifiedInputResolver(Protocol):
    async def resolve(
        self,
        *,
        job_id: UUID,
        request: VerifiedTaskRequest,
        run_generation: int,
    ) -> TrustedVerifiedInput: ...


class VerifiedPipeline(Protocol):
    async def build(
        self,
        *,
        job_id: UUID,
        request: VerifiedTaskRequest,
        resolved_input: TrustedVerifiedInput,
        checkpoints: JobCheckpoints,
    ) -> ReportV22: ...


class VerifiedResultPersister(Protocol):
    async def persist(
        self,
        *,
        job_id: UUID,
        case_id: UUID,
        run_generation: int,
        request: VerifiedTaskRequest,
        resolved_input: TrustedVerifiedInput,
        report: ReportV22,
    ) -> None: ...


class RoutedExecutor(Protocol):
    async def execute(
        self,
        *,
        job_id: UUID,
        request: Any,
        submitted_at: datetime,
        checkpoints: JobCheckpoints,
        cost_ledger: JobCostLedger | None = None,
    ) -> ReportV22: ...


def _invalid_request() -> DeterministicJobError:
    return DeterministicJobError(
        "V22_ANALYSIS_REQUEST_INVALID",
        "The saved analysis request could not be validated.",
    )


def _parse_verified_envelope(request: Any) -> VerifiedRequestEnvelope:
    try:
        if isinstance(request, VerifiedRequestEnvelope):
            raw = request.model_dump(mode="python")
        elif isinstance(request, dict):
            raw = request
        else:
            raise TypeError
        return VerifiedRequestEnvelope.model_validate_json(canonical_json_bytes(raw))
    except (TypeError, ValueError, ValidationError, RecursionError, OverflowError):
        raise _invalid_request() from None


class VerifiedV22Executor:
    """Resolve one frozen graph, build deterministically and persist atomically."""

    def __init__(
        self,
        *,
        resolver: VerifiedInputResolver,
        pipeline: VerifiedPipeline,
        persister: VerifiedResultPersister,
    ) -> None:
        self.resolver = resolver
        self.pipeline = pipeline
        self.persister = persister

    async def execute(
        self,
        *,
        job_id: UUID,
        request: Any,
        submitted_at: datetime,
        checkpoints: JobCheckpoints,
        cost_ledger: JobCostLedger | None = None,
    ) -> ReportV22:
        del submitted_at, cost_ledger
        envelope = _parse_verified_envelope(request)
        verified_request = envelope.verified_request
        if job_id in {
            verified_request.parent_report_id,
            verified_request.gsc_snapshot_id,
            verified_request.ga4_snapshot_id,
            verified_request.public_gbp_snapshot_id,
        }:
            raise _invalid_request() from None

        resolved = await self.resolver.resolve(
            job_id=job_id,
            request=verified_request,
            run_generation=checkpoints.run_generation,
        )
        try:
            trusted_payload = require_trusted_verified_input(
                resolved, run_generation=checkpoints.run_generation
            )
            trusted_payload.validate_request(job_id=job_id, request=verified_request)
        except (TypeError, ValueError, ValidationError, RecursionError, OverflowError):
            raise DeterministicJobError(
                "V22_VERIFIED_INPUT_INVALID",
                "The frozen Verified report inputs could not be validated.",
            ) from None

        report = await self.pipeline.build(
            job_id=job_id,
            request=verified_request,
            resolved_input=resolved,
            checkpoints=checkpoints,
        )
        try:
            checked = ReportV22.model_validate(
                report.model_dump(mode="python") if isinstance(report, ReportV22) else report
            )
            if (
                checked.report_version.report_type != "verified_execution"
                or checked.report_version.report_id != job_id
                or checked.report_version.parent_report_id
                != verified_request.parent_report_id
                or checked.identity.case_id != verified_request.case_id
            ):
                raise ValueError
        except (AttributeError, TypeError, ValueError, ValidationError):
            raise DeterministicJobError(
                "V22_VERIFIED_RESULT_INVALID",
                "The Verified report did not pass validation.",
            ) from None
        await self.persister.persist(
            job_id=job_id,
            case_id=verified_request.case_id,
            run_generation=checkpoints.run_generation,
            request=verified_request,
            resolved_input=resolved,
            report=report,
        )
        return report


class V22ExecutorRouter:
    """Fail-closed dispatcher for the two exact durable request schemas."""

    def __init__(
        self,
        *,
        prospect_executor: RoutedExecutor,
        verified_executor: RoutedExecutor,
    ) -> None:
        self.prospect_executor = prospect_executor
        self.verified_executor = verified_executor

    async def execute(
        self,
        *,
        job_id: UUID,
        request: Any,
        submitted_at: datetime,
        checkpoints: JobCheckpoints,
        cost_ledger: JobCostLedger | None = None,
    ) -> ReportV22:
        try:
            if isinstance(request, AnalysisRequestEnvelope):
                schema = request.schema_version
                parsed: Any = AnalysisRequestEnvelope.model_validate_json(
                    canonical_json_bytes(request.model_dump(mode="python"))
                )
            elif isinstance(request, VerifiedRequestEnvelope):
                schema = request.schema_version
                parsed = _parse_verified_envelope(request)
            elif isinstance(request, dict):
                schema = request.get("schema_version")
                if schema == "v22_analysis_request_envelope_v1":
                    parsed = AnalysisRequestEnvelope.model_validate_json(
                        canonical_json_bytes(request)
                    )
                elif schema == "v22_verified_request_envelope_v1":
                    parsed = _parse_verified_envelope(request)
                else:
                    raise TypeError
            else:
                raise TypeError
        except (TypeError, ValueError, ValidationError, RecursionError, OverflowError):
            raise _invalid_request() from None

        executor = (
            self.prospect_executor
            if schema == "v22_analysis_request_envelope_v1"
            else self.verified_executor
        )
        return await executor.execute(
            job_id=job_id,
            request=parsed,
            submitted_at=submitted_at,
            checkpoints=checkpoints,
            cost_ledger=cost_ledger,
        )
