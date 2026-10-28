"""Internal exact-window collection, not authorization or source persistence.

The caller must resolve an owner-authorized saved record and source binding.
No browser route, queue, token broker or credit settlement is invoked here.
"""
import asyncio
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from app.google_connections_v22.gsc import GscProvider, GscSnapshot, SyncError, evaluate_health
from app.jobs_v22.cost_ledger import JobCostLedger
from app.jobs_v22.digest import verified_request_digest, verified_canonical_json_bytes, request_digest
from app.report_v22.capability import Checksum, ImplementationReference, Label, Timestamp, instant
from app.report_v22.comparison_base import StrictModel
from app.report_v22.comparison_windows import ComparisonWindows, WindowDays, plan_windows
from app.report_v22.implementation_records import ImplementationRecord, implementation_reference


class GscComparisonPlan(StrictModel):
    schema_version: Literal["v22_gsc_comparison_plan_v1"]
    user_id: UUID
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    implementation: ImplementationReference
    action_id: Label
    binding_id: UUID
    resource_id: str = Field(min_length=1, max_length=2048)
    prepared_at: Timestamp
    windows: ComparisonWindows
    state: Literal["waiting", "collectable"]
    expected_requests: Literal[0, 12]
    checksum: Checksum

    @model_validator(mode="after")
    def consistent(self):
        if self.windows.timezone != "America/Los_Angeles" or "\x00" in self.resource_id:
            raise ValueError("invalid GSC scope")
        elapsed = instant(self.prepared_at).astimezone(ZoneInfo(self.windows.timezone)).date() > self.windows.after.end
        if self.state != ("collectable" if elapsed else "waiting") or self.expected_requests != (12 if elapsed else 0):
            raise ValueError("invalid collection eligibility")
        if self.checksum != verified_request_digest(self.model_dump(mode="json", exclude={"checksum"})):
            raise ValueError("plan checksum mismatch")
        return self


class GscComparisonCollection(StrictModel):
    schema_version: Literal["v22_gsc_comparison_collection_v1"]
    plan: GscComparisonPlan
    state: Literal["waiting", "collected"]
    snapshot: GscSnapshot | None
    payload_checksum: Checksum | None
    health_status: Literal["healthy", "unhealthy"] | None
    health_reasons: list[Label]
    checksum: Checksum

    @model_validator(mode="after")
    def consistent(self):
        if self.plan.state == "waiting":
            if self.state != "waiting" or self.snapshot is not None or self.payload_checksum is not None or self.health_status is not None or self.health_reasons:
                raise ValueError("waiting cannot publish collection")
        else:
            s = self.snapshot
            if self.state != "collected" or s is None:
                raise ValueError("missing collection")
            if (s.resource_id != self.plan.resource_id or s.timezone != self.plan.windows.timezone
                    or (s.previous.start_date, s.previous.end_date) != (self.plan.windows.before.start, self.plan.windows.before.end)
                    or (s.current.start_date, s.current.end_date) != (self.plan.windows.after.start, self.plan.windows.after.end)):
                raise ValueError("collection scope mismatch")
            health, reasons = evaluate_health(s)
            if self.payload_checksum != request_digest(s.model_dump(mode="json")) or (self.health_status, self.health_reasons) != (health, reasons):
                raise ValueError("collection checksum or health mismatch")
        if self.checksum != verified_request_digest(self.model_dump(mode="json", exclude={"checksum"})):
            raise ValueError("collection checksum mismatch")
        return self


def prepare_gsc_comparison(record: ImplementationRecord, *, action_id: str, days: WindowDays,
                           binding_id: UUID, resource_id: str, at: str) -> GscComparisonPlan:
    # Round-trip revalidates even an instance changed via model_copy/update.
    record = ImplementationRecord.model_validate_json(verified_canonical_json_bytes(record.model_dump(mode="json")))
    if instant(at) < instant(record.recorded_at):
        raise ValueError("collection preparation predates saved record")
    entry = next((item for item in record.entries if item.action_id == action_id), None)
    if entry is None:
        raise ValueError("action has no saved implementation")
    windows = plan_windows(entry, "America/Los_Angeles", days)
    elapsed = instant(at).astimezone(ZoneInfo(windows.timezone)).date() > windows.after.end
    body = dict(schema_version="v22_gsc_comparison_plan_v1", user_id=str(record.user_id), case_id=str(record.case_id),
        report_id=str(record.report_id), delivery_revision_id=str(record.delivery_revision_id),
        implementation=implementation_reference(record).model_dump(mode="json"), action_id=action_id,
        binding_id=str(binding_id), resource_id=resource_id, prepared_at=at, windows=windows.model_dump(mode="json"),
        state="collectable" if elapsed else "waiting", expected_requests=12 if elapsed else 0)
    body["checksum"] = verified_request_digest(body)
    return GscComparisonPlan.model_validate_json(verified_canonical_json_bytes(body))


async def collect_gsc_comparison(record: ImplementationRecord, *, action_id: str, days: WindowDays,
                                 binding_id: UUID, resource_id: str, at: str, provider: GscProvider,
                                 token: str, cost_ledger: JobCostLedger) -> GscComparisonCollection:
    plan = prepare_gsc_comparison(record, action_id=action_id, days=days, binding_id=binding_id, resource_id=resource_id, at=at)
    snapshot = None
    if plan.state == "collectable":
        if cost_ledger is None:
            raise SyncError("SYNC_COST_LEDGER_REQUIRED")
        try:
            async with asyncio.timeout(240):
                snapshot = await provider.collect_periods(resource_id, token,
                    previous=(plan.windows.before.start, plan.windows.before.end),
                    current=(plan.windows.after.start, plan.windows.after.end), cost_ledger=cost_ledger)
        except TimeoutError:
            raise SyncError("SYNC_TIMEOUT", True) from None
    health, reasons = evaluate_health(snapshot) if snapshot is not None else (None, [])
    body = dict(schema_version="v22_gsc_comparison_collection_v1", plan=plan.model_dump(mode="json"),
        state="collected" if snapshot is not None else "waiting", snapshot=snapshot.model_dump(mode="json") if snapshot is not None else None,
        payload_checksum=request_digest(snapshot.model_dump(mode="json")) if snapshot is not None else None,
        health_status=health, health_reasons=reasons)
    body["checksum"] = verified_request_digest(body)
    return GscComparisonCollection.model_validate_json(verified_canonical_json_bytes(body))
