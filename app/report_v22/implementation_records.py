"""P5 user-reported work, pinned to an immutable approved delivery.

Parsing is not proof that a page changed or that an action caused a metric change.
The caller must retrieve an owner-scoped, completed report and persisted revision.
This module never fetches a page, starts an analysis or changes credit settlement.
"""
from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator

from app.report_v22.review_plan_basis import parse_plan_report, parse_plan_delivery, plan_business, plan_report_checksum
from app.jobs_v22.digest import verified_canonical_json_bytes, verified_request_digest
from app.report_v22.capability import (
    CapabilityReport, Checksum, DeliveryRevision, ImplementationReference,
    Label, StrictModel, Text, Timestamp, instant,
)


class ImplementationEntry(StrictModel):
    action_id: Label
    scope: Text
    implemented_on: str = Field(pattern=r"^[1-9]\d{3}-\d{2}-\d{2}$")
    timezone: str = Field(min_length=1, max_length=100, pattern=r"^(UTC|[A-Za-z_+-]+(?:/[A-Za-z0-9_+.-]+)+)$")
    note: Text | None
    responsible: Label | None

    @model_validator(mode="after")
    def valid_date_and_zone(self):
        date.fromisoformat(self.implemented_on)
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("IANA implementation timezone required") from error
        if any("\x00" in text for text in [self.action_id, self.scope, self.note or "", self.responsible or ""]):
            raise ValueError("NUL text is not supported")
        return self


class ImplementationRecord(StrictModel):
    schema_version: Literal["v22_implementation_record_v1"]
    id: UUID
    user_id: UUID
    case_id: UUID
    report_id: UUID
    report_checksum: Checksum
    delivery_revision_id: UUID
    delivery_revision: int = Field(ge=1, lt=2147483647)
    revision: int = Field(ge=1, lt=2147483647)
    recorded_at: Timestamp
    entries: list[ImplementationEntry] = Field(max_length=3)
    checksum: Checksum

    @model_validator(mode="after")
    def validate_content(self):
        recorded = instant(self.recorded_at)
        if len({item.action_id for item in self.entries}) != len(self.entries):
            raise ValueError("duplicate implementation action")
        for item in self.entries:
            if date.fromisoformat(item.implemented_on) > recorded.astimezone(ZoneInfo(item.timezone)).date():
                raise ValueError("implementation date is later than recording date")
        if self.checksum != verified_request_digest(self.model_dump(mode="json", exclude={"checksum"})):
            raise ValueError("implementation checksum mismatch")
        return self


def parse_implementation_record(value: dict, report: CapabilityReport, *, user_id: UUID, case_id: UUID,
                                delivery_revision_id: UUID, delivery_revision: int,
                                delivery_content: DeliveryRevision) -> ImplementationRecord:
    """Bind trusted persisted source metadata before a future review may freeze it."""
    report = parse_plan_report(report)
    delivery = parse_plan_delivery(delivery_content).validate_report(report)
    record = ImplementationRecord.model_validate_json(verified_canonical_json_bytes(value))
    if report.report_type != "verified_execution" or delivery.status != "approved":
        raise ValueError("completed Verified and approved delivery required")
    if (record.user_id != user_id or record.case_id != case_id or record.report_id != report.id
            or plan_business(report).user_id != user_id or plan_business(report).case_id != case_id
            or record.report_checksum != plan_report_checksum(report)
            or record.delivery_revision_id != delivery_revision_id or record.delivery_revision != delivery_revision
            or delivery.expected_revision + 1 != delivery_revision):
        raise ValueError("implementation source binding mismatch")
    if not {entry.action_id for entry in record.entries} <= {action.id for action in delivery.actions}:
        raise ValueError("implementation action not in approved delivery")
    if instant(record.recorded_at) < instant(report.generated_at):
        raise ValueError("implementation recording predates report")
    return record


def implementation_reference(record: ImplementationRecord) -> ImplementationReference:
    """An exact revision, not 'latest'; review generation is still separately gated."""
    checked = ImplementationRecord.model_validate(record)
    return ImplementationReference(id=checked.id, revision=checked.revision, checksum=checked.checksum)
