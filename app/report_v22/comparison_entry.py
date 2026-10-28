"""Owner-facing metadata and result projections; never expose snapshot bodies."""
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.report_v22.capability import ImplementationReference, Label, Text, Timestamp
from app.report_v22.comparison_base import StrictModel
from app.report_v22.comparison_models import ComparisonResult, State
from app.report_v22.comparison_storage import ComparisonSelection, ComparisonRepository, FrozenComparison, invalid
from app.report_v22.comparison_windows import WindowDays
from app.jobs_v22.errors import DeterministicJobError


class Observation(StrictModel):
    id: UUID
    source: Literal["gsc", "ga4"]
    resource: Text
    coverage_start: date
    coverage_end: date
    fetched_at: Timestamp
    expires_at: Timestamp
    current_eligible: bool


class CheckSummary(StrictModel):
    id: UUID
    implementation: ImplementationReference
    action_id: Label
    days: WindowDays
    source: Literal["gsc", "ga4"]
    state: State
    evaluated_at: Timestamp


class ComparisonContext(StrictModel):
    schema_version: Literal["v22_comparison_context_v1"]
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    implementation: ImplementationReference | None
    sources: list[Observation] = Field(max_length=50)
    history: list[CheckSummary] = Field(max_length=20)


class ComparisonView(StrictModel):
    schema_version: Literal["v22_comparison_view_v1"]
    id: UUID
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    result: ComparisonResult


class EntryCommand(StrictModel):
    operation: Literal["context", "read", "freeze"]
    user_id: UUID
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    comparison_id: UUID | None
    selection: ComparisonSelection | None

    @model_validator(mode="after")
    def shape(self):
        if (self.operation == "context") != (self.comparison_id is None):
            raise ValueError("comparison ID required only for read/freeze")
        if (self.operation == "freeze") != (self.selection is not None):
            raise ValueError("selection required only for freeze")
        return self


class EntryContracts(StrictModel):
    context: ComparisonContext
    view: ComparisonView
    selection: ComparisonSelection


def conflict():
    return DeterministicJobError("V22_COMPARISON_CONFLICT", "The saved record or selected data has changed. Reload before starting a new check.")


class ComparisonEntry:
    def __init__(self, repository: ComparisonRepository):
        self.repository = repository

    async def context(self, command: EntryCommand) -> ComparisonContext:
        raw = await self.repository._rpc("get_v22_comparison_context", dict(p_user_id=str(command.user_id), p_case_id=str(command.case_id),
            p_report_id=str(command.report_id), p_delivery_revision_id=str(command.delivery_revision_id)))
        try:
            result = ComparisonContext.model_validate_json(raw)
            if (result.case_id, result.report_id, result.delivery_revision_id) != (command.case_id, command.report_id, command.delivery_revision_id):
                raise ValueError("context binding mismatch")
            return result
        except (TypeError, ValueError):
            raise invalid() from None

    @staticmethod
    def view(saved: FrozenComparison, command: EntryCommand) -> ComparisonView:
        record = saved.request.implementation
        if (record.user_id, record.case_id, record.report_id, record.delivery_revision_id) != (
                command.user_id, command.case_id, command.report_id, command.delivery_revision_id):
            raise DeterministicJobError("V22_COMPARISON_NOT_FOUND", "This saved check is not available for this report.")
        return ComparisonView(schema_version="v22_comparison_view_v1", id=saved.id, case_id=command.case_id,
            report_id=command.report_id, delivery_revision_id=command.delivery_revision_id, result=saved.result)

    async def run(self, command: EntryCommand) -> ComparisonContext | ComparisonView:
        command = EntryCommand.model_validate(command)
        if command.operation == "context":
            return await self.context(command)
        identity = dict(user_id=command.user_id, case_id=command.case_id, comparison_id=command.comparison_id)
        saved = await self.repository.read(**identity)
        if saved:
            view = self.view(saved, command)
            if command.operation == "freeze" and saved.selection != command.selection:
                raise conflict()
            return view
        if command.operation == "read":
            raise DeterministicJobError("V22_COMPARISON_NOT_FOUND", "This saved check is not available.")
        context = await self.context(command)
        selection = command.selection
        if context.implementation != selection.implementation:
            raise conflict()
        for name in ("before", "after"):
            selected = getattr(selection, f"{name}_snapshot_id")
            if selected is None:
                continue
            source = next((s for s in context.sources if s.id == selected and s.source == selection.scope.source), None)
            if not source or (name == "after" and not source.current_eligible):
                raise conflict()
        return self.view(await self.repository.freeze(**identity, selection=selection), command)
