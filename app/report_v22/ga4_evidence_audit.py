"""Reproducible explanation of a saved observation, not a comparison or new source."""
from datetime import date, timedelta
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.jobs_v22.digest import verified_request_digest
from app.report_v22.capability import Checksum, Timestamp
from app.report_v22.comparison_base import StrictModel
from app.report_v22.comparison_windows import ComparisonWindows
from app.report_v22.ga4_comparison_models import Ga4ComparisonScope, Ga4ExactSnapshotV2, checked_ga4_snapshot
from app.report_v22.ga4_truncation import TruncationReason


class PeriodCoverage(StrictModel):
    period: Literal["previous", "current"]
    expected_dates: int = Field(ge=1, le=90)
    returned_dates: int = Field(ge=0, le=90)
    missing_dates: list[date] = Field(max_length=90)


class ViewLimit(StrictModel):
    period: Literal["previous", "current"]
    view: Literal["totals", "dates", "key_events"]
    truncation_capture: Literal["not_captured", "captured"]
    truncation_reasons: list[TruncationReason] = Field(max_length=20)
    row_limit_reached: bool
    sampled: bool
    thresholded: bool
    other_row_loss: bool
    provider_empty_reason: bool
    restricted_metrics: list[str] = Field(max_length=50)


class EventChange(StrictModel):
    event_name: str = Field(min_length=1, max_length=4096)
    change: Literal["added", "removed", "changed"]
    fields: list[Literal["counting_method", "create_time", "custom", "deletable"]] = Field(max_length=4)


class Ga4EvidenceAudit(StrictModel):
    schema_version: Literal["v22_ga4_evidence_audit_v1"]
    collection_id: UUID
    observation_checksum: Checksum
    scope: Ga4ComparisonScope
    windows: ComparisonWindows
    observed_at: Timestamp
    coverage: list[PeriodCoverage] = Field(min_length=2, max_length=2)
    views: list[ViewLimit] = Field(min_length=6, max_length=6)
    configuration_before_at: Timestamp
    configuration_after_at: Timestamp
    configuration_change_count: int = Field(ge=0, le=8000)
    configuration_changes: list[EventChange] = Field(max_length=30)
    processing_completeness: Literal["not_attested"]
    event_history: Literal["unverified"]
    comparisons_available: Literal[False]
    causal_attribution: Literal[False]


def audit_ga4(collection_id: UUID, snapshot) -> Ga4EvidenceAudit:
    s = checked_ga4_snapshot(snapshot)
    coverage, views = [], []
    checks = {(c.period, c.view): c.reasons for c in s.truncation_checks} if isinstance(s, Ga4ExactSnapshotV2) else {}
    for name in ("previous", "current"):
        p = getattr(s, name)
        expected = [p.start_date + timedelta(days=i) for i in range((p.end_date-p.start_date).days+1)]
        returned = {row.date for row in p.dates.rows}
        coverage.append(PeriodCoverage(period=name, expected_dates=len(expected), returned_dates=len(returned),
            missing_dates=[d for d in expected if d not in returned]))
        for view_name in ("totals", "dates", "key_events"):
            view = getattr(p, view_name); m = view.metadata
            views.append(ViewLimit(period=name, view=view_name,
                truncation_capture="captured" if checks else "not_captured", truncation_reasons=checks.get((name, view_name), []),
                row_limit_reached=view.truncated, sampled=bool(m.sampling), thresholded=m.subject_to_thresholding,
                other_row_loss=m.data_loss_from_other_row, provider_empty_reason=bool(m.empty_reason),
                restricted_metrics=sorted({r.metric_name for r in m.metric_restrictions})))
    before = {e.event_name: e for e in s.configuration_before.events}
    after = {e.event_name: e for e in s.configuration_after.events}
    changes = []
    for name in sorted(before.keys() | after.keys()):
        if name not in before or name not in after:
            changes.append(EventChange(event_name=name, change="added" if name in after else "removed", fields=[]))
        elif before[name] != after[name]:
            fields = [f for f in ("counting_method", "create_time", "custom", "deletable") if getattr(before[name], f) != getattr(after[name], f)]
            changes.append(EventChange(event_name=name, change="changed", fields=fields))
    return Ga4EvidenceAudit(schema_version="v22_ga4_evidence_audit_v1", collection_id=collection_id,
        observation_checksum=verified_request_digest(s.model_dump(mode="json")), scope=s.scope, windows=s.windows,
        observed_at=s.completed_at, coverage=coverage, views=views,
        configuration_before_at=s.configuration_before.observed_at, configuration_after_at=s.configuration_after.observed_at,
        configuration_change_count=len(changes), configuration_changes=changes[:30], processing_completeness="not_attested",
        event_history="unverified", comparisons_available=False, causal_attribution=False)
