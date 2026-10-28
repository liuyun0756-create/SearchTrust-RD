"""Internal P5 contracts. No public route may accept caller-supplied snapshots."""
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import Field, FiniteFloat, field_validator, model_validator

from app.report_v22.capability import Checksum, ImplementationReference, Label, Text, Timestamp, instant
from app.report_v22.comparison_base import StrictModel
from app.report_v22.comparison_windows import ComparisonWindows, WindowDays
from app.report_v22.first_party_findings_models import TrustedFirstPartySnapshot
from app.report_v22.implementation_records import ImplementationRecord

RULESET = "v22_comparison_descriptive_v1"
State = Literal["waiting", "insufficient", "not_comparable", "comparable"]


class ScopeFilter(StrictModel):
    dimension: Literal["page", "query", "device", "country", "landing_page"]
    value: Text


class ComparisonScope(StrictModel):
    source: Literal["gsc", "ga4"]
    dimension: Literal["property", "page", "query", "device", "country", "landing_page"]
    key: Text | None
    channel: Literal["all", "organic_search"]
    filters: list[ScopeFilter] = Field(max_length=4)

    @model_validator(mode="after")
    def target(self):
        if (self.dimension == "property") != (self.key is None):
            raise ValueError("property scope has no key; other scopes require one")
        if self.source == "gsc" and self.channel != "organic_search":
            raise ValueError("GSC measures search, not all traffic")
        return self


class ComparisonRequest(StrictModel):
    schema_version: Literal["v22_comparison_request_v1"]
    implementation: ImplementationRecord
    action_id: Label
    days: WindowDays
    scope: ComparisonScope
    evaluated_at: Timestamp
    before: TrustedFirstPartySnapshot | None
    after: TrustedFirstPartySnapshot | None
    concurrent_changes: Literal["unknown", "reported", "none_reported"]

    @field_validator("before", "after", mode="before")
    @classmethod
    def decode_snapshot(cls, value):
        # Preserve the existing strict JSON date/UUID boundary inside a nullable
        # model union (Pydantic 2.10 otherwise switches nested values to Python).
        if value is None:
            return None
        from app.jobs_v22.digest import canonical_json_bytes
        return TrustedFirstPartySnapshot.model_validate_json(canonical_json_bytes(value))

    @model_validator(mode="after")
    def bound(self):
        from app.jobs_v22.digest import verified_request_digest
        if self.action_id not in {entry.action_id for entry in self.implementation.entries}:
            raise ValueError("selected action has no saved implementation record")
        if instant(self.implementation.recorded_at) > instant(self.evaluated_at):
            raise ValueError("evaluation predates the implementation record")
        for source in (self.before, self.after):
            if source and (source.case_id != self.implementation.case_id or source.source_type != self.scope.source
                    or source.fetched_at > instant(self.evaluated_at)):
                raise ValueError("snapshot identity or evaluation binding mismatch")
        if self.before and self.after and self.before.snapshot_id == self.after.snapshot_id:
            if verified_request_digest(self.before.model_dump(mode="json")) != verified_request_digest(self.after.model_dump(mode="json")):
                raise ValueError("one snapshot identity cannot have two contents")
        return self


class ComparisonSource(StrictModel):
    snapshot_id: UUID
    binding_id: UUID
    payload_checksum: Checksum
    resource_id: Text
    fetched_at: Timestamp
    data_cutoff: date | None = Field(description="Latest observed final GSC date available in this snapshot, not a GA4 finality promise")
    used_through: date | None = Field(description="Last date actually used in a qualified calculation; null when no calculation was published")


class MetricComparison(StrictModel):
    metric: Literal["clicks", "impressions", "ctr"]
    unit: Literal["count", "ratio"]
    state: Literal["comparable", "insufficient"]
    before: FiniteFloat | None
    after: FiniteFloat | None
    absolute_change: FiniteFloat | None
    relative_change: FiniteFloat | None
    percentage_point_change: FiniteFloat | None
    reasons: list[Label]


class ComparisonResult(StrictModel):
    schema_version: Literal["v22_comparison_result_v1"]
    ruleset_version: Literal["v22_comparison_descriptive_v1"]
    input_checksum: Checksum
    implementation: ImplementationReference
    action_id: Label
    evaluated_at: Timestamp
    scope: ComparisonScope
    windows: ComparisonWindows
    state: State
    reasons: list[Label]
    before_source: ComparisonSource | None
    after_source: ComparisonSource | None
    metrics: list[MetricComparison] = Field(max_length=3)
    effect_eligibility: Literal["pending_metric_specific_calibration"]
    effect_conclusion: None
    causal_attribution: Literal[False]
    limitations: list[Text]
    checksum: Checksum

    @model_validator(mode="after")
    def consistent(self):
        from app.jobs_v22.digest import verified_request_digest
        if self.state != "comparable" and self.metrics:
            raise ValueError("unqualified comparison cannot publish deltas")
        if self.state == "comparable" and (self.scope.source != "gsc" or not self.before_source or not self.after_source
                or [item.metric for item in self.metrics] != ["clicks", "impressions", "ctr"]):
            raise ValueError("descriptive source qualification missing")
        if self.state == "comparable" and (self.before_source.used_through != self.windows.before.end or self.after_source.used_through != self.windows.after.end):
            raise ValueError("actual used cutoff differs from selected windows")
        if self.checksum != verified_request_digest(self.model_dump(mode="json", exclude={"checksum"})):
            raise ValueError("comparison result checksum mismatch")
        return self


class ComparisonContracts(StrictModel):
    request: ComparisonRequest
    result: ComparisonResult
