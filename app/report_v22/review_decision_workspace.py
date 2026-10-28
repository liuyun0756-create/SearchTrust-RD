"""Owner-only safe views over frozen evidence; no providers or report writes."""
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter, model_validator

from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.capability import Checksum, Label, Text
from app.report_v22.comparison_base import StrictModel
from app.report_v22.review_decision_models import SaveDecision, DecisionRevision, DecisionHistory, RevisionNumber
from app.report_v22.review_decision_validation import error, parse
from app.report_v22.review_evidence_validation import source_references
from app.report_v22.review_freeze_storage import decode
from app.report_v22.review_workspace import view
from app.report_v22.review_workspace_models import WorkspaceView, Identity

MAX_COMMAND = 72 * 1024
MAX_CONTEXT = 512 * 1024


class ContextCommand(Identity):
    operation: Literal["context"]
    review_id: UUID


class HistoryCommand(Identity):
    operation: Literal["history"]
    review_id: UUID
    cursor: RevisionNumber | None = None


class ReadCommand(Identity):
    operation: Literal["read"]
    review_id: UUID
    decision_id: UUID


class SaveCommand(Identity):
    operation: Literal["save"]
    request: SaveDecision


Command = Annotated[ContextCommand | HistoryCommand | ReadCommand | SaveCommand, Field(discriminator="operation")]
adapter = TypeAdapter(Command)


def parse_command(raw):
    try:
        if len(raw) > MAX_COMMAND:
            raise error("TOO_LARGE")
        decoded = decode(raw)
        json.dumps(decoded, allow_nan=False)
        command = adapter.validate_json(canonical_json_bytes(decoded))
        if command.operation == "save":
            parse(SaveDecision, command.request, 65536)
        return command
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        raise error() from None


class ApprovedWork(StrictModel):
    action_id: Label
    title: Label
    target: Text
    steps: list[Text] = Field(min_length=1, max_length=12)
    completion: list[Text] = Field(min_length=1, max_length=12)
    review: Text


class Reference(StrictModel):
    id: Label
    action_id: Label
    kind: Literal["implementation", "page", "gsc", "ga4"]
    label: Label
    detail: Text


class DecisionContext(StrictModel):
    schema_version: Literal["v22_review_decision_context_v1"] = "v22_review_decision_context_v1"
    evidence_checksum: Checksum
    bundle_checksum: Checksum
    evidence: WorkspaceView
    approved: list[ApprovedWork] = Field(max_length=3)
    references: list[Reference] = Field(max_length=15)

    @model_validator(mode="after")
    def shape(self):
        if self.evidence.state != "saved" or [a.action_id for a in self.approved] != [a.action_id for a in self.evidence.actions]:
            raise ValueError("wrong saved actions")
        pairs = [(r.action_id, r.id) for r in self.references]
        if len(set(pairs)) != len(pairs) or any(r.action_id not in {a.action_id for a in self.approved} for r in self.references):
            raise ValueError("wrong references")
        return self


class DecisionWorkspaceContracts(StrictModel):
    command: Command
    context: DecisionContext
    revision: DecisionRevision
    history: DecisionHistory


def context(saved):
    b = saved.bundle
    refs = []
    entries = {e.action_id: e for e in b.implementation.entries}
    for s in b.selections:
        details = {}
        if s.action_id in entries:
            e = entries[s.action_id]
            details[f"implementation:{b.implementation.id}:{s.action_id}"] = (
                "implementation", "SEO-recorded implementation", f"{e.implemented_on} · {e.timezone}. Recorded by the SEO; not system-verified completion.")
        if s.page:
            for name, p in (("Current", s.page.current), ("Previous", s.page.previous)):
                if p:
                    details[f"page:{p.id}"] = ("page", f"{name} page check", f"{p.observed_at} · {p.plan.target_url} · {p.status}. Static response only.")
        if s.gsc:
            g = s.gsc.result
            details[f"gsc:{s.gsc.id}"] = ("gsc", "Search Console comparison", f"{g.windows.before.start}–{g.windows.before.end} → {g.windows.after.start}–{g.windows.after.end} · {g.windows.timezone} · {g.state}. Review the scope and limits alongside the figures.")
        if s.ga4:
            g = s.ga4
            details[f"ga4:{g.id}"] = ("ga4", "GA4 session observation", f"{g.completed_at} · {g.plan.scope.landing_path or 'All landing paths'} · {g.plan.scope.timezone}. Sessions are not qualified enquiries.")
        if set(details) != set(source_references(b, s)):
            raise error()
        refs.extend(Reference(id=id, action_id=s.action_id, kind=x[0], label=x[1], detail=x[2]) for id, x in sorted(details.items()))
    result = DecisionContext(evidence_checksum=b.evidence_checksum, bundle_checksum=b.checksum,
        evidence=view(b, saved.selection, "saved", saved=saved),
        approved=[ApprovedWork(action_id=a.id, title=a.title, target=a.target, steps=a.steps, completion=a.completion, review=a.review) for a in b.delivery.actions],
        references=refs)
    if len(result.model_dump_json().encode()) > MAX_CONTEXT:
        raise error("TOO_LARGE")
    return result


class DecisionWorkspace:
    def __init__(self, decisions):
        self.decisions = decisions

    async def run(self, c):
        c = parse_command(c.model_dump_json().encode())
        identity = dict(user_id=c.user_id, case_id=c.case_id, report_id=c.report_id)
        if c.operation == "context":
            _, saved = await self.decisions.basis(**identity, review_id=c.review_id)
            return context(saved)
        if c.operation == "save":
            return await self.decisions.save(**identity, request=c.request)
        if c.operation == "read":
            return await self.decisions.read(**identity, review_id=c.review_id, decision_id=c.decision_id)
        return await self.decisions.history(**identity, review_id=c.review_id, cursor=c.cursor)
