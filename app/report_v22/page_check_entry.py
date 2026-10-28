"""Independent owner projections for static checks; never expose a lease or source body."""
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.capability import ImplementationReference, Label, Timestamp, instant
from app.report_v22.implementation_records import ImplementationRecord, implementation_reference
from app.report_v22.page_check_limits import PageCheckLimits
from app.report_v22.page_check_storage import ErrorCode, PageCheckJob, PageCheckRepository, PageCheckSelection, PageCheckSummary, invalid
from app.report_v22.page_implementation_models import Failure, FrozenModel, PageConditionResult, page_url


class PageCheckHistory(FrozenModel):
    id: UUID
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    status: Literal["queued", "running", "completed", "failed"]
    created_at: Timestamp
    completed_at: Timestamp | None
    error_code: ErrorCode | None

    @model_validator(mode="after")
    def state(self):
        if ((self.status in ("completed", "failed")) != (self.completed_at is not None)
                or (self.status == "failed") != (self.error_code is not None)
                or self.completed_at and instant(self.completed_at) < instant(self.created_at)):
            raise ValueError("invalid check state")
        return self


class PageCheckResult(FrozenModel):
    observed_at: Timestamp
    status: Literal["checked", "unable_to_check"]
    failure: Failure | None
    results: list[PageConditionResult] = Field(min_length=1, max_length=5)
    limitation: Literal["static_response_only_rendering_functionality_and_identity_not_verified"]


class PageCheckView(PageCheckHistory):
    schema_version: Literal["v22_page_check_view_v1"]
    selection: PageCheckSelection
    delivery_revision: int = Field(ge=1)
    site_url: str = Field(max_length=2000)
    observation: PageCheckResult | None

    @model_validator(mode="after")
    def result(self):
        if (self.status == "completed") != (self.observation is not None):
            raise ValueError("missing or premature observation")
        page_url(self.selection.target_url, site=self.site_url)
        if self.observation:
            o = self.observation
            if (list(r.condition_id for r in o.results) != list(c.id for c in self.selection.conditions)
                    or not instant(self.created_at) <= instant(o.observed_at) <= instant(self.completed_at)
                    or (o.status == "checked") != (o.failure is None)
                    or any((r.state == "unable_to_check") != (o.status == "unable_to_check") for r in o.results)):
                raise ValueError("inconsistent observation")
        return self


class PageCheckAction(FrozenModel):
    id: Label
    title: Label


class PageCheckContext(FrozenModel):
    schema_version: Literal["v22_page_check_context_v1"]
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    delivery_revision: int = Field(ge=1)
    implementation: ImplementationReference | None
    actions: list[PageCheckAction] = Field(max_length=3)
    site_url: str = Field(max_length=2000)
    eligibility: Literal["eligible", "no_record", "no_actions", "source_changed"]
    history: list[PageCheckHistory] = Field(max_length=20)
    limits: PageCheckLimits


class PageCheckCommand(FrozenModel):
    operation: Literal["context", "read", "request"]
    user_id: UUID
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    check_id: UUID | None
    selection: PageCheckSelection | None

    @model_validator(mode="after")
    def shape(self):
        if (self.operation == "context") != (self.check_id is None) or (self.operation == "request") != (self.selection is not None):
            raise ValueError("invalid page check command")
        return self


class PageCheckRateResponse(FrozenModel):
    code: Literal["PAGE_CHECK_RATE_LIMIT"]
    limits: PageCheckLimits


class PageCheckContracts(FrozenModel):
    context: PageCheckContext
    view: PageCheckView
    selection: PageCheckSelection
    rate: PageCheckRateResponse


class ContextInput(FrozenModel):
    record: ImplementationRecord | None
    actions: list[PageCheckAction] = Field(max_length=3)
    site_url: str = Field(max_length=2000)
    eligible: bool
    delivery_revision: int = Field(ge=1)
    history: list[PageCheckSummary] = Field(max_length=20)
    limits: PageCheckLimits


def not_found():
    return DeterministicJobError("V22_PAGE_CHECK_NOT_FOUND", "This page check is not available for this report.")


def conflict():
    return DeterministicJobError("V22_PAGE_CHECK_CONFLICT", "Reload the saved record and confirmed website.")


def bound(item, command):
    return (item.user_id, item.case_id, item.report_id, item.delivery_revision_id) == (
        command.user_id, command.case_id, command.report_id, command.delivery_revision_id)


class PageCheckEntry:
    def __init__(self, repository: PageCheckRepository):
        self.repository = repository

    @staticmethod
    def view(job, command):
        job = PageCheckJob.model_validate_json(job.model_dump_json())
        if not bound(job, command):
            raise not_found()
        summary = {k: getattr(job, k) for k in PageCheckHistory.model_fields}
        o = job.observation
        return PageCheckView(**summary, schema_version="v22_page_check_view_v1", selection=job.selection,
            delivery_revision=job.plan.binding.delivery_revision, site_url=job.plan.binding.site_url,
            observation=PageCheckResult(observed_at=o.observed_at, status=o.status, failure=o.failure,
                results=list(o.results), limitation=o.limitation) if o else None)

    async def context(self, command):
        raw = await self.repository._rpc("get_v22_page_check_context", dict(p_user_id=str(command.user_id),
            p_case_id=str(command.case_id), p_report_id=str(command.report_id), p_delivery_revision_id=str(command.delivery_revision_id)))
        try:
            data = ContextInput.model_validate_json(raw)
            record = data.record
            if (record and (not bound(record, command) or record.delivery_revision != data.delivery_revision)
                    or len({h.id for h in data.history}) != len(data.history)
                    or any(not bound(h, command) for h in data.history)
                    or len({a.id for a in data.actions}) != len(data.actions)
                    or {a.id for a in data.actions} != ({e.action_id for e in record.entries} if record else set())
                    or data.eligible and not data.actions
                    or data.site_url != page_url(data.site_url)):
                raise ValueError("context binding")
        except (ValueError, TypeError):
            raise invalid() from None
        return PageCheckContext(schema_version="v22_page_check_context_v1", case_id=command.case_id,
            report_id=command.report_id, delivery_revision_id=command.delivery_revision_id,
            delivery_revision=data.delivery_revision, implementation=implementation_reference(record) if record else None,
            actions=data.actions, site_url=data.site_url, limits=data.limits,
            eligibility="eligible" if data.eligible else "no_record" if not record else "no_actions" if not data.actions else "source_changed",
            history=[PageCheckHistory(**h.model_dump(exclude={"user_id"})) for h in data.history])

    async def run(self, command):
        command = PageCheckCommand.model_validate_json(command.model_dump_json())
        if command.operation == "context":
            return await self.context(command)
        identity = dict(user_id=command.user_id, case_id=command.case_id, check_id=command.check_id)
        saved = await self.repository.read_check(**identity)
        if saved:
            view = self.view(saved, command)
            if command.operation == "request" and saved.selection != command.selection:
                raise conflict()
            return view
        if command.operation == "read":
            raise not_found()
        context = await self.context(command)
        # Route binding is checked BEFORE request_check can insert anything.
        if (context.eligibility != "eligible" or context.implementation != command.selection.implementation
                or command.selection.action_id not in {a.id for a in context.actions}):
            raise conflict()
        job = await self.repository.request_check(**identity, selection=command.selection,
            report_id=command.report_id, delivery_revision_id=command.delivery_revision_id)
        return self.view(job, command)
