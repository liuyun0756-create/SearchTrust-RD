"""Owner-workspace contracts. No raw bundle, private notes or provider payload in views."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter, model_validator

from app.report_v22.capability import Checksum, Label, Timestamp
from app.report_v22.comparison_base import StrictModel
from app.report_v22.review_freeze_models import ReviewFreezeSelection
from app.report_v22.review_projection_models import ActionFacts, PriorDiagnosis

Kind = Literal["deliveries", "implementations", "history", "page", "gsc", "ga4"]


class Cursor(StrictModel):
    at: Timestamp
    id: UUID
    revision: int = Field(ge=0, le=2147483646)


class Target(StrictModel):
    delivery_id: UUID
    implementation_id: UUID
    implementation_revision: int = Field(ge=1, le=2147483646)


class Identity(StrictModel):
    user_id: UUID
    case_id: UUID
    report_id: UUID


class ContextCommand(Identity):
    operation: Literal["context"]
    target: Target


class ListCommand(Identity):
    operation: Literal["candidates", "history", "versions"]
    kind: Kind
    delivery_id: UUID | None = None
    target: Target | None = None
    action_id: Label | None = None
    cursor: Cursor | None = None

    @model_validator(mode="after")
    def shape(self):
        if self.operation == "candidates":
            valid = self.kind in ("page", "gsc", "ga4") and self.target is not None and self.action_id is not None and self.delivery_id is None
        elif self.operation == "history":
            valid = self.kind == "history" and self.target is None and self.action_id is None and self.delivery_id is None
        else:
            valid = self.kind in ("deliveries", "implementations") and self.target is None and self.action_id is None and ((self.kind == "implementations") == (self.delivery_id is not None))
        if not valid:
            raise ValueError("invalid directory command")
        return self


class SourceCommand(Identity):
    operation: Literal["source"]
    target: Target
    action_id: Label
    kind: Literal["page", "gsc", "ga4"]
    source_id: UUID


class ReadCommand(Identity):
    operation: Literal["read"]
    review_id: UUID


class PreviewCommand(Identity):
    operation: Literal["preview"]
    selection: ReviewFreezeSelection


class SaveCommand(Identity):
    operation: Literal["save"]
    review_id: UUID
    selection: ReviewFreezeSelection
    confirmation_fingerprint: Checksum


Command = Annotated[ContextCommand | ListCommand | SourceCommand | ReadCommand | PreviewCommand | SaveCommand, Field(discriminator="operation")]
command_adapter = TypeAdapter(Command)


class DirectoryItem(Cursor):
    delivery_id: UUID
    delivery_revision: int = Field(ge=1)


class Directory(StrictModel):
    case_id: UUID
    report_id: UUID
    kind: Kind
    items: list[DirectoryItem] = Field(max_length=20)
    next_cursor: Cursor | None

    @model_validator(mode="after")
    def page(self):
        if len({(x.id, x.revision) for x in self.items}) != len(self.items):
            raise ValueError("duplicate directory item")
        if self.next_cursor and (len(self.items) != 20 or self.next_cursor.model_dump() != self.items[-1].model_dump(include={"at", "id", "revision"})):
            raise ValueError("invalid continuation")
        return self


class WorkspaceView(StrictModel):
    schema_version: Literal["v22_review_workspace_v1"] = "v22_review_workspace_v1"
    state: Literal["context", "source", "preview", "saved"]
    case_id: UUID
    report_id: UUID
    report_version: int = Field(ge=1)
    site_url: str = Field(max_length=2000)
    selection: ReviewFreezeSelection
    evaluated_at: Timestamp
    prior_diagnosis: PriorDiagnosis
    actions: list[ActionFacts] = Field(max_length=3)
    confirmation_fingerprint: Checksum | None = None
    review_id: UUID | None = None
    frozen_at: Timestamp | None = None

    @model_validator(mode="after")
    def shape(self):
        if self.report_id != self.selection.report.id or [a.action_id for a in self.actions] != [a.action_id for a in self.selection.actions]:
            raise ValueError("view selection mismatch")
        if (self.state == "preview") != (self.confirmation_fingerprint is not None):
            raise ValueError("invalid confirmation state")
        if (self.state == "saved") != (self.review_id is not None) or (self.state == "saved") != (self.frozen_at is not None):
            raise ValueError("invalid saved state")
        for a, s in zip(self.actions, self.selection.actions):
            if (a.page is not None) != (s.page_current_id is not None) or (a.gsc is not None) != (s.gsc_id is not None) or (a.ga4 is not None) != (s.ga4_id is not None):
                raise ValueError("missing selected facts")
        return self


class WorkspaceContracts(StrictModel):
    command: Command
    view: WorkspaceView
    directory: Directory
