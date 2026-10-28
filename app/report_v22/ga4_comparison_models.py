"""Separate GA4 collection contract. Not a ga4_sync_v1 or an effect result."""
from datetime import date, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from app.google_connections_v22.ga4 import (
    PROPERTY, Ga4Provider, OverviewView, DateView, KeyEventView, KeyEventConfig,
)
from app.jobs_v22.digest import verified_request_digest
from app.report_v22.capability import Checksum, ImplementationReference, Label, Timestamp, instant
from app.report_v22.comparison_base import StrictModel
from app.report_v22.comparison_windows import ComparisonWindows
from app.report_v22.ga4_truncation import TruncationCheck


class Ga4ComparisonScope(StrictModel):
    resource_id: str = Field(pattern=r"^properties/[0-9]+$", max_length=128)
    hosts: list[str] = Field(min_length=1, max_length=2)
    timezone: Label
    channel: Literal["organic_search"]
    landing_path: str | None = Field(max_length=2048)

    @model_validator(mode="after")
    def valid_scope(self):
        ZoneInfo(self.timezone)
        if not PROPERTY.fullmatch(self.resource_id) or self.hosts != sorted(set(self.hosts)) or any(
            not Ga4Provider._valid_host(host) for host in self.hosts
        ):
            raise ValueError("invalid GA4 website scope")
        path = self.landing_path
        if path is not None and (not path.startswith("/") or path.startswith("//") or any(
            ord(c) < 33 or c in "?#\\" for c in path
        )):
            raise ValueError("exact landing path required, not a URL or query")
        return self


class Ga4ComparisonPlan(StrictModel):
    schema_version: Literal["v22_ga4_comparison_plan_v1"]
    user_id: UUID
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    implementation: ImplementationReference
    action_id: Label
    binding_id: UUID
    scope: Ga4ComparisonScope
    prepared_at: Timestamp
    windows: ComparisonWindows
    state: Literal["waiting", "collectable"]
    expected_report_requests: Literal[0, 6]
    max_configuration_requests: Literal[0, 20]
    checksum: Checksum

    @model_validator(mode="after")
    def consistent(self):
        if self.windows.timezone != self.scope.timezone:
            raise ValueError("GA4 window timezone mismatch")
        elapsed = instant(self.prepared_at).astimezone(ZoneInfo(self.scope.timezone)).date() > self.windows.after.end
        if (self.state, self.expected_report_requests, self.max_configuration_requests) != (
            ("collectable", 6, 20) if elapsed else ("waiting", 0, 0)
        ):
            raise ValueError("invalid GA4 calendar eligibility")
        if self.checksum != verified_request_digest(self.model_dump(mode="json", exclude={"checksum"})):
            raise ValueError("GA4 plan checksum mismatch")
        return self


class Ga4ExactPeriod(StrictModel):
    start_date: date
    end_date: date
    totals: OverviewView
    dates: DateView
    key_events: KeyEventView

    @model_validator(mode="after")
    def consistent(self):
        if not 1 <= (self.end_date-self.start_date).days+1 <= 90:
            raise ValueError("invalid GA4 period")
        if len(self.totals.rows) > 1 or len(self.dates.rows) > 90 or len(self.key_events.rows) > 100:
            raise ValueError("GA4 response row limit")
        dates = [row.date for row in self.dates.rows]
        events = [row.event_name for row in self.key_events.rows]
        if len(dates) != len(set(dates)) or any(not self.start_date <= day <= self.end_date for day in dates):
            raise ValueError("duplicate or out-of-range GA4 date")
        if len(events) != len(set(events)) or any(any(ord(c) < 32 for c in name) for name in events):
            raise ValueError("duplicate GA4 event")
        for view in (self.totals, self.dates, self.key_events):
            for sample in view.metadata.sampling:
                if sample.sampling_space_size == 0 or sample.samples_read_count > sample.sampling_space_size:
                    raise ValueError("invalid GA4 sampling metadata")
        return self


class Ga4ConfigurationObservation(StrictModel):
    # This is a current settings observation, NEVER the historical period's settings.
    observed_at: Timestamp
    events: list[KeyEventConfig] = Field(max_length=4000)

    @model_validator(mode="after")
    def consistent(self):
        names = [e.event_name for e in self.events]
        if names != sorted(set(names)) or any(any(ord(c) < 32 for c in n) for n in names):
            raise ValueError("invalid GA4 configuration identities")
        if any(e.create_time is not None and (e.create_time.tzinfo is None or e.create_time > instant(self.observed_at)) for e in self.events):
            raise ValueError("event creation after observation or without timezone")
        return self


class Ga4ExactSnapshot(StrictModel):
    schema_version: Literal["ga4_comparison_observation_v1"]
    scope: Ga4ComparisonScope
    windows: ComparisonWindows
    started_at: Timestamp
    completed_at: Timestamp
    previous: Ga4ExactPeriod
    current: Ga4ExactPeriod
    configuration_before: Ga4ConfigurationObservation
    configuration_after: Ga4ConfigurationObservation

    @model_validator(mode="after")
    def consistent(self):
        times = [self.started_at, self.configuration_before.observed_at, self.configuration_after.observed_at, self.completed_at]
        if [instant(t) for t in times] != sorted(instant(t) for t in times):
            raise ValueError("GA4 observation times out of order")
        if self.windows.timezone != self.scope.timezone:
            raise ValueError("GA4 observation timezone mismatch")
        for period, window in ((self.previous, self.windows.before), (self.current, self.windows.after)):
            if (period.start_date, period.end_date) != (window.start, window.end):
                raise ValueError("GA4 observation period mismatch")
            if any(getattr(period, v).metadata.time_zone != self.scope.timezone for v in ("totals", "dates", "key_events")):
                raise ValueError("GA4 response timezone differs from planned property timezone")
        return self


class Ga4ExactSnapshotV2(Ga4ExactSnapshot):
    schema_version: Literal["ga4_comparison_observation_v2"]
    truncation_checks: list[TruncationCheck] = Field(min_length=6, max_length=6)

    @model_validator(mode="after")
    def all_checks(self):
        expected = [(p, v) for p in ("previous", "current") for v in ("totals", "dates", "key_events")]
        if [(c.period, c.view) for c in self.truncation_checks] != expected:
            raise ValueError("missing, duplicate or unordered truncation checks")
        return self


def checked_ga4_snapshot(snapshot):
    model = Ga4ExactSnapshotV2 if snapshot.schema_version == "ga4_comparison_observation_v2" else Ga4ExactSnapshot
    return model.model_validate_json(snapshot.model_dump_json())


class Ga4MetricQualification(StrictModel):
    metric: Literal["sessions", "engaged_sessions", "engagement_rate", "key_events", "session_key_event_rate"]
    state: Literal["not_comparable"]
    reasons: list[Label] = Field(min_length=1, max_length=30)


class Ga4Qualification(StrictModel):
    ruleset_version: Literal["v22_ga4_qualification_foundation_v1", "v22_ga4_qualification_truncation_v2"]
    observation_checksum: Checksum
    complete_daily_observations: bool
    processing_completeness: Literal["not_attested"]
    event_configuration: Literal["none_observed", "changed_during_collection", "same_at_collection"]
    event_history: Literal["unverified"]
    metrics: list[Ga4MetricQualification] = Field(min_length=5, max_length=5)
    effect_conclusion: None
    causal_attribution: Literal[False]


def qualify_ga4(snapshot: Ga4ExactSnapshot) -> Ga4Qualification:
    # Reparse mutated model instances and never accept caller-provided qualification.
    snapshot = checked_ga4_snapshot(snapshot)
    periods = (snapshot.previous, snapshot.current)
    complete = all({r.date for r in p.dates.rows} == {
        p.start_date+timedelta(days=i) for i in range((p.end_date-p.start_date).days+1)
    } for p in periods)
    common = ["GA4_COMPLETENESS_NOT_ATTESTED"]
    if not complete:
        common.append("GA4_DAILY_OBSERVATIONS_INCOMPLETE")
    if any(not p.totals.rows for p in periods):
        common.append("GA4_AGGREGATE_NOT_OBSERVED")
    before, after = snapshot.configuration_before.events, snapshot.configuration_after.events
    config = "changed_during_collection" if before != after else "same_at_collection" if before else "none_observed"
    event_reasons = ["GA4_EVENT_HISTORY_NOT_VERIFIED"]
    if config == "changed_during_collection": event_reasons.append("GA4_EVENT_CONFIGURATION_CHANGED")
    if not before or not after: event_reasons.append("GA4_NO_CONFIGURED_KEY_EVENTS")
    if any(e.counting_method == "COUNTING_METHOD_UNSPECIFIED" for e in [*before, *after]):
        event_reasons.append("GA4_EVENT_COUNTING_METHOD_UNKNOWN")
    if any(e.create_time is None for e in [*before, *after]):
        event_reasons.append("GA4_EVENT_CREATION_TIME_UNKNOWN")
    if any(e.create_time is not None and e.create_time.astimezone(ZoneInfo(snapshot.scope.timezone)).date() >= snapshot.windows.before.start for e in [*before, *after]):
        event_reasons.append("GA4_EVENT_NOT_PRESENT_FOR_FULL_BASELINE")
    if any(p.key_events.truncated for p in periods): event_reasons.append("GA4_KEY_EVENT_TRUNCATED")
    if any(not p.key_events.rows for p in periods): event_reasons.append("GA4_EVENT_ACTIVITY_NOT_OBSERVED")
    known = {e.event_name for e in before} & {e.event_name for e in after}
    if any(r.event_name not in known for p in periods for r in p.key_events.rows):
        event_reasons.append("GA4_REPORTED_EVENT_NOT_IN_CURRENT_CONFIGURATION")
    metrics = []
    names = {"sessions": "sessions", "engaged_sessions": "engagedSessions", "engagement_rate": "engagementRate",
        "key_events": "keyEvents", "session_key_event_rate": "sessionKeyEventRate"}
    for metric, api_name in names.items():
        events = metric in ("key_events", "session_key_event_rate")
        reasons = [*common, *(event_reasons if events else [])]
        views = ("totals", "dates", "key_events") if events else ("totals", "dates")
        metadata = [getattr(p, v).metadata for p in periods for v in views]
        if any(m.sampling or m.subject_to_thresholding or m.data_loss_from_other_row for m in metadata):
            reasons.append("GA4_REPORT_RESTRICTED")
        if any(m.empty_reason for m in metadata): reasons.append("GA4_PROVIDER_EMPTY_REASON")
        if isinstance(snapshot, Ga4ExactSnapshotV2) and any(c.reasons and c.view in views for c in snapshot.truncation_checks):
            reasons.append("GA4_DATA_TRUNCATION_REPORTED")
        required = {api_name} | ({"sessions"} if metric in ("engagement_rate", "session_key_event_rate") else set())
        if any(r.metric_name in required for m in metadata for r in m.metric_restrictions):
            reasons.append("GA4_METRIC_RESTRICTED")
        if metric in ("engagement_rate", "session_key_event_rate") and any(p.totals.rows and p.totals.rows[0].sessions == 0 for p in periods):
            reasons.append("GA4_ZERO_SESSION_DENOMINATOR")
        metrics.append(Ga4MetricQualification(metric=metric, state="not_comparable", reasons=reasons))
    return Ga4Qualification(ruleset_version="v22_ga4_qualification_truncation_v2" if isinstance(snapshot, Ga4ExactSnapshotV2) else "v22_ga4_qualification_foundation_v1",
        observation_checksum=verified_request_digest(snapshot.model_dump(mode="json")),
        complete_daily_observations=complete, processing_completeness="not_attested", event_configuration=config,
        event_history="unverified", metrics=metrics, effect_conclusion=None, causal_attribution=False)


class Ga4ComparisonCollection(StrictModel):
    schema_version: Literal["v22_ga4_comparison_collection_v1"]
    plan: Ga4ComparisonPlan
    state: Literal["waiting", "collected"]
    snapshot: Ga4ExactSnapshotV2 | Ga4ExactSnapshot | None
    qualification: Ga4Qualification | None
    checksum: Checksum

    @model_validator(mode="after")
    def consistent(self):
        if self.plan.state == "waiting":
            if self.state != "waiting" or self.snapshot is not None or self.qualification is not None:
                raise ValueError("waiting GA4 plan cannot publish data")
        else:
            s = self.snapshot
            if self.state != "collected" or s is None or s.scope != self.plan.scope or s.windows != self.plan.windows:
                raise ValueError("GA4 collection scope mismatch")
            if instant(s.started_at) < instant(self.plan.prepared_at):
                raise ValueError("collection precedes preparation")
            if self.qualification != qualify_ga4(s):
                raise ValueError("GA4 qualification does not reproduce")
        if self.checksum != verified_request_digest(self.model_dump(mode="json", exclude={"checksum"})):
            raise ValueError("GA4 collection checksum mismatch")
        return self
