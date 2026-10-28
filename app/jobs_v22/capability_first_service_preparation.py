"""First-service collection on the already reserved task, never a Prospect."""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.jobs_v22.capability_acquisition_input import _build_collected_input
from app.jobs_v22.capability_dispatch import CapabilityDispatch, authorize_dispatch
from app.jobs_v22.cost_models import validate_cost_counters_snapshot
from app.jobs_v22.digest import canonical_json_bytes, verified_canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.first_service_collection_models import FirstServiceCollectionEnvelope
from app.report_v22.capability import BusinessScope
from app.report_v22.first_party_findings_models import TrustedFirstPartySnapshot
from app.report_v22.models import StrictModel


class FirstServicePreparation(StrictModel):
    schema_version: Literal["v22_first_service_collection_preparation_v1"]
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
    snapshots: list[TrustedFirstPartySnapshot] = Field(max_length=2)
    dispatch: CapabilityDispatch | None

    @field_validator("cost_counters")
    @classmethod
    def bounded_costs(cls, value):
        return validate_cost_counters_snapshot(value)

    @model_validator(mode="after")
    def bound(self):
        if (self.business.user_id != self.user_id or self.business.case_id != self.case_id
                or self.deadline_at <= self.created_at):
            raise ValueError("Invalid preparation")
        if self.dispatch is not None:
            if self.snapshots:
                raise ValueError("Ready dispatch must not expose private payloads")
            for field in ("user_id", "case_id", "job_id", "idempotency_key", "created_at", "deadline_at", "run_generation"):
                if getattr(self, field) != getattr(self.dispatch, field):
                    raise ValueError("Invalid dispatch")
            if self.dispatch.request.capability_request.report_type != "verified_execution":
                raise ValueError("Invalid dispatch mode")
        elif (len(self.snapshots) != 2 or {s.source_type for s in self.snapshots} != {"gsc", "ga4"}
                or len({s.snapshot_id for s in self.snapshots}) != 2
                or len({s.binding_id for s in self.snapshots}) != 2
                or any(s.case_id != self.case_id or s.health_status != "healthy" or s.identity_match_status != "matched"
                    or s.fetched_at > self.created_at for s in self.snapshots)):
            raise ValueError("Invalid frozen private sources")
        return self


def parse_first_service_request(request):
    try:
        return FirstServiceCollectionEnvelope.model_validate_json(canonical_json_bytes(request))
    except (ValueError, TypeError, AttributeError):
        raise DeterministicJobError("V22_FIRST_SERVICE_PREPARATION_INVALID",
            "The first-service collection request could not be prepared.") from None


def validate_receipt(receipt, envelope):
    req = envelope.collection_request
    scope = receipt.business
    if (receipt.request_checksum != verified_request_digest(envelope.model_dump(mode="json"))
            or receipt.case_id != req.case_id or scope.site_url != str(req.business_identity.site_url)
            or scope.primary_service != req.primary_service or scope.target_market != req.target_market.display_name
            or sorted(scope.competitor_sites) != sorted(str(c.website_url) for c in req.competitors)):
        raise ValueError("Unbound collection request")


async def begin_first_service_preparation(repository, *, user_id, job_id, idempotency_key, business_version_id, request):
    envelope = parse_first_service_request(request)
    canonical = verified_canonical_json_bytes(envelope.model_dump(mode="json"))
    if len(canonical) > 150_000:
        raise DeterministicJobError("V22_FIRST_SERVICE_PREPARATION_INVALID", "The collection request is too large.")
    raw = await repository.rpc.post("begin_v22_first_service_collection", dict(p_user_id=str(user_id),
        p_case_id=str(envelope.collection_request.case_id), p_job_id=str(job_id), p_idempotency_key=idempotency_key,
        p_business_version_id=str(business_version_id), p_request_canonical=canonical.decode()))
    try:
        receipt = FirstServicePreparation.model_validate_json(canonical_json_bytes(raw))
        validate_receipt(receipt, envelope)
        if (receipt.user_id != user_id or receipt.job_id != job_id or receipt.idempotency_key != idempotency_key
                or receipt.business.id != business_version_id):
            raise ValueError()
        return receipt, envelope
    except (ValueError, TypeError, AttributeError):
        raise repository.rpc.invalid() from None


class CapabilityFirstServicePreparer:
    def __init__(self, *, repository, collector, discovery_verifier, clock=None):
        self.repository, self.collector, self.discovery_verifier = repository, collector, discovery_verifier
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def prepare(self, *, user_id, job_id, idempotency_key, business_version_id, request, checkpoints, cost_ledger):
        receipt, envelope = await begin_first_service_preparation(self.repository, user_id=user_id, job_id=job_id,
            idempotency_key=idempotency_key, business_version_id=business_version_id, request=request)
        if receipt.dispatch is not None:
            return receipt.dispatch
        now = self.clock()
        if (not receipt.created_at <= now < receipt.deadline_at
                or any(s.expires_at <= now for s in receipt.snapshots)
                or checkpoints.run_generation != receipt.run_generation or cost_ledger is None
                or cost_ledger.job_id != job_id or cost_ledger.job_created_at != receipt.created_at):
            raise DeterministicJobError("V22_FIRST_SERVICE_PREPARATION_INVALID",
                "The original sources, deadline and execution budget are required.")
        if receipt.cost_counters and not await cost_ledger.redis.exists(cost_ledger.key):
            raise TransientJobError("CAPABILITY_RECOVERY_REQUIRED", "This analysis is awaiting recovery.")
        link = await self.discovery_verifier.verify(discovery_id=envelope.competitor_discovery.discovery_id,
            request=envelope.collection_request, now=now)
        if link != envelope.competitor_discovery:
            raise DeterministicJobError("V22_FIRST_SERVICE_PREPARATION_INVALID", "The confirmed discovery changed.")
        collected = await self.collector.collect_confirmed_sources(job_id=job_id, request=envelope.collection_request,
            discovery_link=link, submitted_at=receipt.created_at, checkpoints=checkpoints, cost_ledger=cost_ledger)
        manifest, value = _build_collected_input(user_id=user_id, job_id=job_id, business=receipt.business,
            request=envelope.collection_request, collected=collected, created_at=receipt.created_at,
            deadline_at=receipt.deadline_at, now=self.clock(), private_snapshots=receipt.snapshots)
        await self.repository.prepare(manifest=manifest, request=value, run_generation=receipt.run_generation)
        result = await authorize_dispatch(self.repository, user_id=user_id, case_id=receipt.case_id,
            job_id=job_id, idempotency_key=idempotency_key)
        if (result.request.capability_request.manifest_id != manifest.id
                or result.request.capability_request.input_checksum != manifest.input_checksum):
            raise self.repository.rpc.invalid()
        return result


class ClaimedFirstServicePreparation(StrictModel):
    outcome: Literal["claimed", "ready"]
    attempt_count: int = Field(ge=0, le=10)
    request: dict
    preparation: FirstServicePreparation

    @model_validator(mode="after")
    def bound(self):
        validate_receipt(self.preparation, parse_first_service_request(self.request))
        if ((self.outcome == "ready") != (self.preparation.dispatch is not None)
                or (self.outcome == "claimed" and self.attempt_count < 1)):
            raise ValueError("Invalid first-service claim")
        return self


async def enqueue_first_service_preparation(*, preparer, queue, submit_dispatch, **identity):
    receipt, _ = await begin_first_service_preparation(preparer.repository, **identity)
    if receipt.dispatch is not None:
        return await submit_dispatch(user_id=receipt.user_id, job_id=receipt.job_id,
            idempotency_key=receipt.idempotency_key, request=receipt.dispatch.request)
    await queue.enqueue(receipt.job_id, receipt.run_generation)
    return receipt
