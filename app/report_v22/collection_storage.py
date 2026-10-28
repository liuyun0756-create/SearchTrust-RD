"""Owner-bound durable exact-window requests, separate from ordinary syncs.

Internal only. An authenticated entry must supply the owner; checksums are not
authorization. No task is requested by a read, and no automatic retry is made.
"""
import json
from typing import Literal
from uuid import UUID

from pydantic import model_validator

from app.jobs_v22.digest import canonical_json_bytes, verified_canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.capability import ImplementationReference, Label, Timestamp, instant
from app.report_v22.comparison_base import StrictModel
from app.report_v22.comparison_collection import GscComparisonPlan, GscComparisonCollection, prepare_gsc_comparison
from app.report_v22.comparison_storage import ComparisonRepository
from app.report_v22.comparison_windows import WindowDays
from app.report_v22.implementation_records import ImplementationRecord, implementation_reference


class CollectionSelection(StrictModel):
    implementation: ImplementationReference
    action_id: Label
    days: WindowDays
    binding_id: UUID


class CollectionJob(StrictModel):
    schema_version: Literal["v22_collection_job_v1"]
    id: UUID
    user_id: UUID
    case_id: UUID
    connection_id: UUID
    selection: CollectionSelection
    record: ImplementationRecord
    plan: GscComparisonPlan
    status: Literal["queued", "running", "succeeded", "failed"]
    lease_id: UUID | None
    lease_expires_at: Timestamp | None
    created_at: Timestamp
    completed_at: Timestamp | None
    error_code: Label | None
    collection: GscComparisonCollection | None

    @model_validator(mode="after")
    def consistent(self):
        s, r = self.selection, self.record
        if (r.user_id, r.case_id, implementation_reference(r)) != (self.user_id, self.case_id, s.implementation):
            raise ValueError("collection record binding mismatch")
        expected = prepare_gsc_comparison(r, action_id=s.action_id, days=s.days, binding_id=s.binding_id,
            resource_id=self.plan.resource_id, at=self.plan.prepared_at)
        if self.plan != expected or self.plan.state != "collectable":
            raise ValueError("collection plan mismatch")
        if instant(self.created_at) < instant(self.plan.prepared_at):
            raise ValueError("collection creation predates plan")
        if self.status == "queued":
            if self.lease_id is not None or self.lease_expires_at is not None:
                raise ValueError("unclaimed task has a lease")
        elif self.lease_id is None or self.lease_expires_at is None:
            raise ValueError("missing collection lease")
        terminal = self.status in ("succeeded", "failed")
        if terminal != (self.completed_at is not None):
            raise ValueError("invalid completion state")
        if self.completed_at and instant(self.completed_at) < instant(self.created_at):
            raise ValueError("invalid completion instant")
        if self.status == "succeeded":
            if self.collection is None or self.collection.plan != self.plan or self.collection.state != "collected":
                raise ValueError("missing or inconsistent collection")
        elif self.collection is not None:
            raise ValueError("unfinished task published material")
        if (self.status == "failed") != (self.error_code is not None):
            raise ValueError("invalid failure state")
        return self


def invalid():
    return DeterministicJobError("V22_COLLECTION_INVALID", "Collection storage returned inconsistent information.")


class CollectionRepository(ComparisonRepository):
    """Reuse bounded private SQL transport, not ordinary source persistence."""

    @staticmethod
    def checked(raw: bytes, *, collection_id: UUID, user_id: UUID | None = None,
                case_id: UUID | None = None) -> CollectionJob:
        try:
            job = CollectionJob.model_validate_json(raw)
            if (job.id != collection_id or (user_id is not None and job.user_id != user_id)
                    or (case_id is not None and job.case_id != case_id)):
                raise ValueError("collection identity mismatch")
            return job
        except (ValueError, TypeError, OverflowError):
            raise invalid() from None

    async def read_collection(self, *, user_id: UUID, case_id: UUID, collection_id: UUID) -> CollectionJob | None:
        raw = await self._rpc("get_v22_collection", dict(p_user_id=str(user_id), p_case_id=str(case_id), p_id=str(collection_id)))
        if raw.strip() == b"null":
            return None
        return self.checked(raw, collection_id=collection_id, user_id=user_id, case_id=case_id)

    async def request_collection(self, *, user_id: UUID, case_id: UUID, collection_id: UUID,
                                 selection: CollectionSelection) -> CollectionJob:
        selection = CollectionSelection.model_validate_json(selection.model_dump_json())
        saved = await self.read_collection(user_id=user_id, case_id=case_id, collection_id=collection_id)
        if saved is not None:
            if saved.selection != selection:
                raise DeterministicJobError("V22_COLLECTION_CONFLICT", "This request already belongs to a different collection.")
            return saved
        raw = await self._rpc("resolve_v22_collection_input", dict(p_user_id=str(user_id), p_case_id=str(case_id),
            p_selection=selection.model_dump(mode="json")))
        try:
            data = json.loads(raw)
            record = ImplementationRecord.model_validate_json(verified_canonical_json_bytes(data["record"]))
            if (record.user_id, record.case_id, implementation_reference(record)) != (user_id, case_id, selection.implementation):
                raise ValueError("resolved record mismatch")
            plan = prepare_gsc_comparison(record, action_id=selection.action_id, days=selection.days,
                binding_id=selection.binding_id, resource_id=data["resource_id"], at=data["prepared_at"])
        except (ValueError, TypeError, KeyError, OverflowError):
            raise invalid() from None
        if plan.state != "collectable":
            raise DeterministicJobError("V22_COLLECTION_WAITING", "The selected after window has not ended yet.")
        raw = await self._rpc("request_v22_collection", dict(p_user_id=str(user_id), p_case_id=str(case_id),
            p_id=str(collection_id), p_selection=selection.model_dump(mode="json"), p_plan=plan.model_dump(mode="json"),
            p_canonical_plan=verified_canonical_json_bytes(plan.model_dump(mode="json", exclude={"checksum"})).decode()))
        job = self.checked(raw, collection_id=collection_id, user_id=user_id, case_id=case_id)
        if job.selection != selection:
            raise invalid()
        return job

    async def claim_collection(self, collection_id: UUID) -> CollectionJob | None:
        raw = await self._rpc("claim_v22_collection", dict(p_id=str(collection_id)))
        if raw.strip() == b"null":
            return None
        job = self.checked(raw, collection_id=collection_id)
        if job.status != "running":
            raise invalid()
        return job

    async def finish_collection(self, job: CollectionJob, collection: GscComparisonCollection) -> CollectionJob:
        job = CollectionJob.model_validate_json(job.model_dump_json())
        collection = GscComparisonCollection.model_validate_json(collection.model_dump_json())
        if job.status != "running" or collection.plan != job.plan:
            raise invalid()
        raw = await self._rpc("finish_v22_collection", dict(p_id=str(job.id), p_lease_id=str(job.lease_id),
            p_payload=collection.model_dump(mode="json"),
            p_canonical_snapshot=canonical_json_bytes(collection.snapshot.model_dump(mode="json")).decode(),
            p_canonical_payload=verified_canonical_json_bytes(collection.model_dump(mode="json", exclude={"checksum"})).decode()))
        saved = self.checked(raw, collection_id=job.id, user_id=job.user_id, case_id=job.case_id)
        if saved.selection != job.selection or saved.plan != job.plan or saved.lease_id != job.lease_id:
            raise invalid()
        if saved.status not in ("succeeded", "failed") or (saved.status == "succeeded" and saved.collection != collection):
            raise invalid()
        return saved

    async def fail_collection(self, job: CollectionJob, code: str):
        await self._rpc("fail_v22_collection", dict(p_id=str(job.id), p_lease_id=str(job.lease_id), p_code=code))
