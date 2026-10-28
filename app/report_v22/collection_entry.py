"""Owner report-bound collection projections, never source bodies or tokens."""
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.capability import ImplementationReference, Text, Timestamp, Label
from app.report_v22.comparison_base import StrictModel
from app.report_v22.comparison_windows import ComparisonWindows, WindowDays, plan_windows
from app.report_v22.implementation_records import ImplementationRecord, implementation_reference
from app.report_v22.collection_storage import CollectionRepository, CollectionJob, CollectionSelection, invalid


class CollectionView(StrictModel):
    schema_version: Literal["v22_collection_view_v1"]
    id: UUID
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    selection: CollectionSelection
    windows: ComparisonWindows
    resource: Text
    status: Literal["queued", "running", "succeeded", "failed"]
    created_at: Timestamp
    completed_at: Timestamp | None
    error_code: Label | None


class CollectionBinding(StrictModel):
    id: UUID
    resource: Text


class WindowPreview(StrictModel):
    action_id: Label
    days: WindowDays
    windows: ComparisonWindows
    elapsed: bool


class CollectionContext(StrictModel):
    schema_version: Literal["v22_collection_context_v1"]
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    implementation: ImplementationReference | None
    bindings: list[CollectionBinding] = Field(max_length=20)
    jobs: list[CollectionView] = Field(max_length=20)
    previews: list[WindowPreview] = Field(max_length=12)


class CollectionCommand(StrictModel):
    operation: Literal["context", "read", "request"]
    user_id: UUID
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    collection_id: UUID | None
    selection: CollectionSelection | None

    @model_validator(mode="after")
    def shape(self):
        if (self.operation == "context") != (self.collection_id is None) or (self.operation == "request") != (self.selection is not None):
            raise ValueError("invalid collection command")
        return self


class CollectionContracts(StrictModel):
    context: CollectionContext
    view: CollectionView
    selection: CollectionSelection


def not_found():
    return DeterministicJobError("V22_COLLECTION_NOT_FOUND", "This collection is not available for this report.")


class CollectionEntry:
    def __init__(self, repository: CollectionRepository):
        self.repository = repository

    @staticmethod
    def bound(view, command):
        if (view.case_id, view.report_id, view.delivery_revision_id) != (command.case_id, command.report_id, command.delivery_revision_id):
            raise not_found()
        return view

    def view(self, job: CollectionJob, command: CollectionCommand):
        if job.user_id != command.user_id:
            raise not_found()
        return self.bound(CollectionView(schema_version="v22_collection_view_v1",id=job.id,case_id=job.case_id,
            report_id=job.record.report_id,delivery_revision_id=job.record.delivery_revision_id,selection=job.selection,
            windows=job.plan.windows,resource=job.plan.resource_id,status=job.status,created_at=job.created_at,
            completed_at=job.completed_at,error_code=job.error_code), command)

    async def context(self, command):
        raw = await self.repository._rpc("get_v22_collection_context",dict(p_user_id=str(command.user_id),p_case_id=str(command.case_id),
            p_report_id=str(command.report_id),p_delivery_revision_id=str(command.delivery_revision_id)))
        try:
            data=json.loads(raw)
            if not isinstance(data,dict):raise ValueError("invalid context")
            record=data.pop("implementation_record")
            if (record is None)!=(data["implementation"] is None):raise ValueError("missing record reference")
            previews=[]
            if record is not None:
                record=ImplementationRecord.model_validate_json(json.dumps(record))
                if (record.user_id,record.case_id,record.report_id,record.delivery_revision_id)!=(command.user_id,command.case_id,command.report_id,command.delivery_revision_id):
                    raise ValueError("record binding mismatch")
                if implementation_reference(record).model_dump(mode="json")!=data["implementation"]:raise ValueError("record reference mismatch")
                today=datetime.now(timezone.utc).astimezone(ZoneInfo("America/Los_Angeles")).date()
                for entry in record.entries:
                    for days in (7,14,28,90):
                        windows=plan_windows(entry,"America/Los_Angeles",days)
                        previews.append(WindowPreview(action_id=entry.action_id,days=days,windows=windows,elapsed=today>windows.after.end).model_dump(mode="json"))
            value=CollectionContext.model_validate_json(json.dumps(dict(schema_version="v22_collection_context_v1",
                case_id=str(command.case_id),report_id=str(command.report_id),delivery_revision_id=str(command.delivery_revision_id),previews=previews,**data)))
            for job in value.jobs:
                self.bound(job,command)
            return value
        except (ValueError,TypeError,KeyError):
            raise invalid() from None

    async def run(self, command: CollectionCommand):
        command=CollectionCommand.model_validate_json(command.model_dump_json())
        if command.operation=="context":
            return await self.context(command)
        identity=dict(user_id=command.user_id,case_id=command.case_id,collection_id=command.collection_id)
        saved=await self.repository.read_collection(**identity)
        if saved:
            result=self.view(saved,command)
            if command.operation=="request" and saved.selection!=command.selection:
                raise DeterministicJobError("V22_COLLECTION_CONFLICT","This request has different selections.")
            return result
        if command.operation=="read":
            raise not_found()
        context=await self.context(command)
        if context.implementation!=command.selection.implementation or not any(b.id==command.selection.binding_id for b in context.bindings):
            raise DeterministicJobError("V22_COLLECTION_CONFLICT","Reload the saved record and confirmed sources.")
        return self.view(await self.repository.request_collection(**identity,selection=command.selection),command)
