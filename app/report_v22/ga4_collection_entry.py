"""Owner-bound GA4 projections: scope/windows/qualification, never source rows."""
import asyncio
import json
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.capability import ImplementationReference, Label, Timestamp, instant
from app.report_v22.comparison_base import StrictModel
from app.report_v22.comparison_windows import ComparisonWindows, WindowDays, plan_windows
from app.report_v22.ga4_collection_storage import Ga4CollectionRepository, Ga4CollectionJob, Ga4CollectionSelection, invalid
from app.report_v22.ga4_comparison_models import Ga4ComparisonScope, Ga4Qualification
from app.report_v22.implementation_records import ImplementationRecord, implementation_reference


class Ga4CollectionView(StrictModel):
    schema_version: Literal["v22_ga4_collection_view_v1"]
    id: UUID
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    selection: Ga4CollectionSelection
    scope: Ga4ComparisonScope
    windows: ComparisonWindows
    status: Literal["queued", "running", "succeeded", "failed"]
    created_at: Timestamp
    completed_at: Timestamp | None
    error_code: Label | None
    qualification: Ga4Qualification | None


class Ga4Binding(StrictModel):
    binding_id: UUID
    scope_snapshot_id: UUID
    observed_at: Timestamp
    scope: Ga4ComparisonScope


class Ga4Preview(StrictModel):
    binding_id: UUID
    action_id: Label
    days: WindowDays
    windows: ComparisonWindows
    elapsed: bool


class Ga4CollectionContext(StrictModel):
    schema_version: Literal["v22_ga4_collection_context_v1"]
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    implementation: ImplementationReference | None
    bindings: list[Ga4Binding] = Field(max_length=20)
    jobs: list[Ga4CollectionView] = Field(max_length=20)
    previews: list[Ga4Preview] = Field(max_length=240)


class Ga4CollectionCommand(StrictModel):
    operation: Literal["context", "read", "request"]
    user_id: UUID
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    collection_id: UUID | None
    selection: Ga4CollectionSelection | None

    @model_validator(mode="after")
    def shape(self):
        if (self.operation == "context") != (self.collection_id is None) or (self.operation == "request") != (self.selection is not None):
            raise ValueError("invalid GA4 command")
        return self


class Ga4CollectionContracts(StrictModel):
    context: Ga4CollectionContext
    view: Ga4CollectionView
    selection: Ga4CollectionSelection


class Candidate(StrictModel):
    binding_id: UUID
    scope_snapshot_id: UUID
    observed_at: Timestamp


class ContextInput(StrictModel):
    record: ImplementationRecord | None
    candidates: list[Candidate] = Field(max_length=20)
    job_ids: list[UUID] = Field(max_length=20)


def not_found():
    return DeterministicJobError("V22_GA4_COLLECTION_NOT_FOUND", "This collection is not available for this report.")


class Ga4CollectionEntry:
    def __init__(self, repository: Ga4CollectionRepository):
        self.repository = repository

    def view(self, job: Ga4CollectionJob, command: Ga4CollectionCommand):
        # Revalidate even internal model instances before exposing qualification.
        job = Ga4CollectionJob.model_validate_json(job.model_dump_json())
        if (job.user_id,job.case_id,job.record.report_id,job.record.delivery_revision_id) != (
            command.user_id,command.case_id,command.report_id,command.delivery_revision_id): raise not_found()
        return Ga4CollectionView(schema_version="v22_ga4_collection_view_v1",id=job.id,case_id=job.case_id,
            report_id=job.record.report_id,delivery_revision_id=job.record.delivery_revision_id,selection=job.selection,
            scope=job.plan.scope,windows=job.plan.windows,status=job.status,created_at=job.created_at,
            completed_at=job.completed_at,error_code=job.error_code,
            qualification=job.collection.qualification if job.collection else None)

    async def context(self, command):
        raw=await self.repository._rpc("get_v22_ga4_collection_context",dict(p_user_id=str(command.user_id),p_case_id=str(command.case_id),
            p_report_id=str(command.report_id),p_delivery_revision_id=str(command.delivery_revision_id)))
        try:
            data=ContextInput.model_validate_json(raw)
            if len(set(data.job_ids))!=len(data.job_ids) or len({c.binding_id for c in data.candidates})!=len(data.candidates): raise ValueError("duplicates")
            record=data.record
            if record and (record.user_id,record.case_id,record.report_id,record.delivery_revision_id)!=(
                command.user_id,command.case_id,command.report_id,command.delivery_revision_id): raise ValueError("record binding")
        except (ValueError,TypeError): raise invalid() from None
        bindings=[];previews=[]
        if record and record.entries:
            for candidate in data.candidates:
                selection=Ga4CollectionSelection(implementation=implementation_reference(record),action_id=record.entries[0].action_id,
                    days=28,binding_id=candidate.binding_id,scope_snapshot_id=candidate.scope_snapshot_id,landing_path=None)
                try:
                    plan=await self.repository.resolve_plan(user_id=command.user_id,case_id=command.case_id,selection=selection)
                except DeterministicJobError as error:
                    if error.error_code=="V22_COMPARISON_REJECTED": continue
                    raise
                bindings.append(Ga4Binding(**candidate.model_dump(),scope=plan.scope))
                today=instant(plan.prepared_at).astimezone(ZoneInfo(plan.scope.timezone)).date()
                for entry in record.entries:
                    for days in (7,14,28,90):
                        windows=plan_windows(entry,plan.scope.timezone,days)
                        previews.append(Ga4Preview(binding_id=candidate.binding_id,action_id=entry.action_id,
                            days=days,windows=windows,elapsed=today>windows.after.end))
        jobs=await asyncio.gather(*(self.repository.read_collection(user_id=command.user_id,case_id=command.case_id,collection_id=i) for i in data.job_ids))
        if any(j is None for j in jobs): raise invalid()
        return Ga4CollectionContext(schema_version="v22_ga4_collection_context_v1",case_id=command.case_id,
            report_id=command.report_id,delivery_revision_id=command.delivery_revision_id,
            implementation=implementation_reference(record) if record else None,bindings=bindings,previews=previews,
            jobs=[self.view(j,command) for j in jobs])

    async def run(self, command: Ga4CollectionCommand):
        command=Ga4CollectionCommand.model_validate_json(command.model_dump_json())
        if command.operation=="context": return await self.context(command)
        identity=dict(user_id=command.user_id,case_id=command.case_id,collection_id=command.collection_id)
        saved=await self.repository.read_collection(**identity)
        if saved:
            view=self.view(saved,command)
            if command.operation=="request" and saved.selection!=command.selection:
                raise DeterministicJobError("V22_GA4_COLLECTION_CONFLICT","This request has different selections.")
            return view
        if command.operation=="read": raise not_found()
        context=await self.context(command)
        if context.implementation!=command.selection.implementation or not any(
            b.binding_id==command.selection.binding_id and b.scope_snapshot_id==command.selection.scope_snapshot_id for b in context.bindings):
            raise DeterministicJobError("V22_GA4_COLLECTION_CONFLICT","Reload the saved record and confirmed sources.")
        return self.view(await self.repository.request_collection(**identity,selection=command.selection),command)
