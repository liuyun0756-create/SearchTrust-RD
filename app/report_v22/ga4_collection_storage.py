"""Internal, owner-bound GA4 exact collection persistence; no public entry yet."""
import json
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.google_connections_v22.ga4 import Ga4Snapshot
from app.jobs_v22.digest import request_digest, verified_canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.capability import ImplementationReference, Label, Timestamp, instant
from app.report_v22.comparison_base import StrictModel
from app.report_v22.comparison_storage import ComparisonRepository
from app.report_v22.comparison_windows import WindowDays
from app.report_v22.ga4_comparison_collection import prepare_ga4_comparison
from app.report_v22.ga4_comparison_models import Ga4ComparisonScope, Ga4ComparisonPlan, Ga4ComparisonCollection
from app.report_v22.implementation_records import ImplementationRecord, implementation_reference


class Ga4CollectionSelection(StrictModel):
    implementation: ImplementationReference
    action_id: Label
    days: WindowDays
    binding_id: UUID
    scope_snapshot_id: UUID
    landing_path: str | None = Field(max_length=2048)

    @model_validator(mode="after")
    def exact_path(self):
        path = self.landing_path
        if path is not None and (not path.startswith("/") or path.startswith("//") or any(
            ord(c) < 33 or c in "?#\\" for c in path
        )):
            raise ValueError("exact landing path required")
        return self


class Ga4CollectionJob(StrictModel):
    schema_version: Literal["v22_ga4_collection_job_v1"]
    id: UUID
    user_id: UUID
    case_id: UUID
    connection_id: UUID
    selection: Ga4CollectionSelection
    record: ImplementationRecord
    plan: Ga4ComparisonPlan
    status: Literal["queued", "running", "succeeded", "failed"]
    lease_id: UUID | None
    lease_expires_at: Timestamp | None
    created_at: Timestamp
    completed_at: Timestamp | None
    error_code: Label | None
    collection: Ga4ComparisonCollection | None

    @model_validator(mode="after")
    def consistent(self):
        s, r = self.selection, self.record
        if (r.user_id, r.case_id, implementation_reference(r)) != (self.user_id, self.case_id, s.implementation):
            raise ValueError("GA4 record binding mismatch")
        expected = prepare_ga4_comparison(r, action_id=s.action_id, days=s.days, binding_id=s.binding_id,
            scope=self.plan.scope, at=self.plan.prepared_at)
        if self.plan != expected or self.plan.state != "collectable" or self.plan.scope.landing_path != s.landing_path:
            raise ValueError("GA4 collection plan mismatch")
        if instant(self.created_at) < instant(self.plan.prepared_at):
            raise ValueError("GA4 creation predates plan")
        if self.status == "queued":
            if self.lease_id is not None or self.lease_expires_at is not None:
                raise ValueError("unclaimed task has lease")
        elif self.lease_id is None or self.lease_expires_at is None:
            raise ValueError("missing lease")
        if (self.status in ("succeeded", "failed")) != (self.completed_at is not None):
            raise ValueError("invalid completion state")
        if self.completed_at and instant(self.completed_at) < instant(self.created_at):
            raise ValueError("invalid completion instant")
        if self.status == "succeeded":
            if self.collection is None or self.collection.plan != self.plan or self.collection.state != "collected":
                raise ValueError("missing collection")
            if instant(self.collection.snapshot.completed_at) > instant(self.completed_at):
                raise ValueError("observation after persistence")
        elif self.collection is not None:
            raise ValueError("unfinished task published data")
        if (self.status == "failed") != (self.error_code is not None):
            raise ValueError("invalid failure state")
        return self


def invalid():
    return DeterministicJobError("V22_GA4_COLLECTION_INVALID", "GA4 collection storage returned inconsistent information.")


class Ga4CollectionRepository(ComparisonRepository):
    """Bounded private SQL transport; callers must provide authenticated owner ID."""

    @staticmethod
    def checked(raw, *, collection_id, user_id=None, case_id=None):
        try:
            job = Ga4CollectionJob.model_validate_json(raw)
            if (job.id != collection_id or (user_id is not None and job.user_id != user_id)
                    or (case_id is not None and job.case_id != case_id)):
                raise ValueError("GA4 identity mismatch")
            return job
        except (ValueError, TypeError, KeyError, OverflowError):
            raise invalid() from None

    async def read_collection(self, *, user_id, case_id, collection_id):
        raw = await self._rpc("get_v22_ga4_collection", dict(p_user_id=str(user_id), p_case_id=str(case_id), p_id=str(collection_id)))
        return None if raw.strip() == b"null" else self.checked(raw, collection_id=collection_id, user_id=user_id, case_id=case_id)

    async def request_collection(self, *, user_id, case_id, collection_id, selection: Ga4CollectionSelection):
        selection = Ga4CollectionSelection.model_validate_json(selection.model_dump_json())
        saved = await self.read_collection(user_id=user_id, case_id=case_id, collection_id=collection_id)
        if saved:
            if saved.selection != selection:
                raise DeterministicJobError("V22_GA4_COLLECTION_CONFLICT", "This request has different selections.")
            return saved
        plan = await self.resolve_plan(user_id=user_id, case_id=case_id, selection=selection)
        if plan.state != "collectable":
            raise DeterministicJobError("V22_GA4_COLLECTION_WAITING", "The selected after window has not ended yet.")
        raw = await self._rpc("request_v22_ga4_collection", dict(p_user_id=str(user_id), p_case_id=str(case_id), p_id=str(collection_id),
            p_selection=selection.model_dump(mode="json"), p_plan=plan.model_dump(mode="json"),
            p_canonical_plan=verified_canonical_json_bytes(plan.model_dump(mode="json", exclude={"checksum"})).decode()))
        job = self.checked(raw, collection_id=collection_id, user_id=user_id, case_id=case_id)
        if job.selection != selection: raise invalid()
        return job

    async def resolve_plan(self, *, user_id, case_id, selection: Ga4CollectionSelection):
        """Read-only authorized scope resolution, including not-yet-elapsed windows."""
        selection = Ga4CollectionSelection.model_validate_json(selection.model_dump_json())
        raw = await self._rpc("resolve_v22_ga4_collection_input", dict(p_user_id=str(user_id), p_case_id=str(case_id),
            p_selection=selection.model_dump(mode="json")))
        try:
            data = json.loads(raw)
            record = ImplementationRecord.model_validate_json(json.dumps(data["record"]))
            source = data["scope_source"]
            snapshot = Ga4Snapshot.model_validate_json(json.dumps(source["normalized_payload"]))
            if (record.user_id, record.case_id, implementation_reference(record)) != (user_id, case_id, selection.implementation):
                raise ValueError("resolved record mismatch")
            if (source["snapshot_id"], source["case_id"], source["binding_id"], source["source_type"], source["schema_version"]) != (
                str(selection.scope_snapshot_id), str(case_id), str(selection.binding_id), "ga4", "ga4_sync_v1"
            ) or request_digest(source["normalized_payload"]) != source["payload_checksum"]:
                raise ValueError("scope source mismatch")
            scope = Ga4ComparisonScope.model_validate_json(json.dumps(data["scope"]))
            zones = {getattr(period, view).metadata.time_zone for period in (snapshot.previous, snapshot.current)
                for view in ("totals", "landing_pages", "dates", "key_events")}
            if (scope.resource_id != snapshot.resource_id or scope.hosts != sorted(snapshot.host_filter)
                    or zones != {scope.timezone} or scope.landing_path != selection.landing_path
                    or source["external_resource_id"] != scope.resource_id):
                raise ValueError("scope differs from observed property")
            plan = prepare_ga4_comparison(record, action_id=selection.action_id, days=selection.days,
                binding_id=selection.binding_id, scope=scope, at=data["prepared_at"])
        except (ValueError, TypeError, KeyError, OverflowError):
            raise invalid() from None
        return plan

    async def claim_collection(self, collection_id):
        raw = await self._rpc("claim_v22_ga4_collection", dict(p_id=str(collection_id)))
        if raw.strip() == b"null": return None
        job = self.checked(raw, collection_id=collection_id)
        if job.status != "running": raise invalid()
        return job

    async def finish_collection(self, job: Ga4CollectionJob, collection: Ga4ComparisonCollection):
        job = Ga4CollectionJob.model_validate_json(job.model_dump_json())
        collection = Ga4ComparisonCollection.model_validate_json(collection.model_dump_json())
        if job.status != "running" or collection.plan != job.plan: raise invalid()
        raw = await self._rpc("finish_v22_ga4_collection", dict(p_id=str(job.id), p_lease_id=str(job.lease_id),
            p_payload=collection.model_dump(mode="json"),
            p_canonical_snapshot=verified_canonical_json_bytes(collection.snapshot.model_dump(mode="json")).decode(),
            p_canonical_payload=verified_canonical_json_bytes(collection.model_dump(mode="json", exclude={"checksum"})).decode()))
        saved = self.checked(raw, collection_id=job.id, user_id=job.user_id, case_id=job.case_id)
        if (saved.selection != job.selection or saved.plan != job.plan or saved.lease_id != job.lease_id
                or saved.status not in ("succeeded", "failed") or (saved.status == "succeeded" and saved.collection != collection)):
            raise invalid()
        return saved

    async def fail_collection(self, job, code):
        await self._rpc("fail_v22_ga4_collection", dict(p_id=str(job.id), p_lease_id=str(job.lease_id), p_code=code))
