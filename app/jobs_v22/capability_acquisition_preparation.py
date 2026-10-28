"""Internal acquisition preparation seam; no public route or new debit here."""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.competitors_v22.selection import AnalysisRequestEnvelope
from app.jobs_v22.capability_acquisition_input import build_acquisition_input
from app.jobs_v22.capability_dispatch import CapabilityDispatch, authorize_dispatch
from app.jobs_v22.cost_models import validate_cost_counters_snapshot
from app.jobs_v22.digest import canonical_json_bytes, verified_canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.capability import BusinessScope
from app.report_v22.models import StrictModel


class AcquisitionPreparation(StrictModel):
    schema_version: Literal["v22_acquisition_preparation_v1"]
    user_id: UUID
    case_id: UUID
    job_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    business: BusinessScope
    request_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    created_at: AwareDatetime
    deadline_at: AwareDatetime
    run_generation: int = Field(ge=1)
    cost_counters: dict[str, int | float]
    dispatch: CapabilityDispatch | None

    @field_validator("cost_counters")
    @classmethod
    def bounded_costs(cls, value):
        return validate_cost_counters_snapshot(value)

    @model_validator(mode="after")
    def consistent(self):
        if (self.business.user_id != self.user_id or self.business.case_id != self.case_id
                or self.deadline_at <= self.created_at):
            raise ValueError("Invalid preparation receipt")
        if self.dispatch is not None:
            for field in ("user_id", "case_id", "job_id", "idempotency_key", "created_at", "deadline_at", "run_generation"):
                if getattr(self, field) != getattr(self.dispatch, field):
                    raise ValueError("Invalid prepared dispatch")
            if self.dispatch.request.capability_request.report_type != "prospect":
                raise ValueError("Invalid preparation mode")
        return self


def parse_acquisition_request(request):
    try:
        value = AnalysisRequestEnvelope.model_validate_json(canonical_json_bytes(request))
        if value.analyze_request.report_type != "prospect":
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise DeterministicJobError("V22_ACQUISITION_PREPARATION_INVALID",
            "This acquisition request could not be prepared.") from None


async def begin_acquisition_preparation(repository, *, user_id, job_id, idempotency_key,
                                        business_version_id, request):
    envelope = parse_acquisition_request(request)
    canonical = verified_canonical_json_bytes(envelope.model_dump(mode="json"))
    if len(canonical) > 150_000:
        raise DeterministicJobError("V22_ACQUISITION_PREPARATION_INVALID", "This acquisition request is too large.")
    raw = await repository.rpc.post("begin_v22_acquisition_preparation", dict(
        p_user_id=str(user_id), p_case_id=str(envelope.analyze_request.case_id), p_job_id=str(job_id),
        p_idempotency_key=idempotency_key, p_business_version_id=str(business_version_id),
        p_request_canonical=canonical.decode()))
    try:
        receipt = AcquisitionPreparation.model_validate_json(canonical_json_bytes(raw))
        req = envelope.analyze_request
        scope = receipt.business
        if (receipt.user_id != user_id or receipt.case_id != req.case_id or receipt.job_id != job_id
                or receipt.idempotency_key != idempotency_key or scope.id != business_version_id
                or receipt.request_checksum != verified_request_digest(envelope.model_dump(mode="json"))
                or scope.site_url != str(req.business_identity.site_url) or scope.primary_service != req.primary_service
                or scope.target_market != req.target_market.display_name
                or sorted(scope.competitor_sites) != sorted(str(c.website_url) for c in req.competitors)):
            raise ValueError()
        return receipt, envelope
    except (ValueError, TypeError, AttributeError):
        raise repository.rpc.invalid() from None


class CapabilityAcquisitionPreparer:
    """Called by a future intake worker with its original checkpoints/budget.

    The existing collector is reused via collect_sources, never execute: this
    phase must not generate, persist or charge for a legacy report first.
    """
    def __init__(self, *, repository, collector, discovery_verifier, clock=None):
        self.repository = repository
        self.collector = collector
        self.discovery_verifier = discovery_verifier
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def prepare(self, *, user_id, job_id, idempotency_key, business_version_id,
                      request, checkpoints, cost_ledger):
        receipt, envelope = await begin_acquisition_preparation(self.repository, user_id=user_id,
            job_id=job_id, idempotency_key=idempotency_key, business_version_id=business_version_id, request=request)
        # This also recovers a successful prepare whose HTTP response was lost.
        if receipt.dispatch is not None:
            return receipt.dispatch
        if (not receipt.created_at <= self.clock() < receipt.deadline_at
                or checkpoints.run_generation != receipt.run_generation
                or cost_ledger is None or cost_ledger.job_id != job_id
                or cost_ledger.job_created_at != receipt.created_at):
            raise DeterministicJobError("V22_ACQUISITION_PREPARATION_INVALID",
                "This analysis cannot resume with a different deadline or execution budget.")
        if receipt.cost_counters and not await cost_ledger.redis.exists(cost_ledger.key):
            raise TransientJobError("CAPABILITY_RECOVERY_REQUIRED", "This analysis is awaiting recovery.")
        link = await self.discovery_verifier.verify(discovery_id=envelope.competitor_discovery.discovery_id,
            request=envelope.analyze_request, now=self.clock())
        if link != envelope.competitor_discovery:
            raise DeterministicJobError("V22_ACQUISITION_PREPARATION_INVALID", "The confirmed discovery changed.")
        collected = await self.collector.collect_sources(job_id=job_id, request=envelope,
            submitted_at=receipt.created_at, checkpoints=checkpoints, cost_ledger=cost_ledger)
        manifest, value = build_acquisition_input(user_id=user_id, job_id=job_id, business=receipt.business,
            request=envelope.analyze_request, collected=collected, created_at=receipt.created_at,
            deadline_at=receipt.deadline_at, now=self.clock())
        # Do not compensate on a timeout: SQL may have committed. The next begin
        # recovers its dispatch; the database deadline handles genuine orphans.
        await self.repository.prepare(manifest=manifest, request=value, run_generation=receipt.run_generation)
        result = await authorize_dispatch(self.repository, user_id=user_id, case_id=receipt.case_id,
            job_id=job_id, idempotency_key=idempotency_key)
        if (result.request.capability_request.manifest_id != manifest.id
                or result.request.capability_request.input_checksum != manifest.input_checksum):
            raise self.repository.rpc.invalid()
        return result
