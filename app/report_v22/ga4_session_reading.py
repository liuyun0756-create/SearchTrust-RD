"""Read saved session counts without promoting original comparison qualification."""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from app.jobs_v22.digest import verified_request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.capability import Checksum, ImplementationReference, Label, Timestamp, instant
from app.report_v22.comparison_base import StrictModel
from app.report_v22.comparison_windows import ComparisonWindows
from app.report_v22.ga4_collection_storage import Ga4CollectionJob, Ga4CollectionRepository, invalid
from app.report_v22.ga4_comparison_models import Ga4ComparisonScope, Ga4ExactSnapshotV2, checked_ga4_snapshot

MAX_COUNT = 9007199254740991
Reason = Literal["TRUNCATION_NOT_CAPTURED", "WINDOW_NOT_ELAPSED", "TOTAL_NOT_RETURNED",
                 "DATES_NOT_RETURNED", "SAMPLED", "THRESHOLDED", "OTHER_ROW_LOSS",
                 "EMPTY_REASON", "TRUNCATED", "SESSIONS_RESTRICTED"]


def relative_percent(before: int, after: int) -> str | None:
    if before == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 50
        value = (Decimal(after - before) * 100 / Decimal(before)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return format(value.copy_abs() if value == 0 else value, ".2f")


class Ga4SessionCommand(StrictModel):
    user_id: UUID
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    collection_id: UUID


class Ga4SessionReading(Ga4SessionCommand):
    schema_version: Literal["v22_ga4_session_snapshot_reading_v1"]
    observation_checksum: Checksum
    implementation: ImplementationReference
    action_id: Label
    scope: Ga4ComparisonScope
    windows: ComparisonWindows
    started_at: Timestamp
    completed_at: Timestamp
    state: Literal["available", "withheld"]
    reasons: list[Reason] = Field(max_length=10)
    before: int | None = Field(ge=0, le=MAX_COUNT)
    after: int | None = Field(ge=0, le=MAX_COUNT)
    difference: int | None = Field(ge=-MAX_COUNT, le=MAX_COUNT)
    relative_change_percent: str | None = Field(pattern=r"^-?(0|[1-9][0-9]*)\.[0-9]{2}$", max_length=24)
    processing_completeness: Literal["not_attested"]
    measurement_history: Literal["unverified"]
    effect: None
    causal_attribution: Literal[False]

    @model_validator(mode="after")
    def consistent(self):
        if len(set(self.reasons)) != len(self.reasons) or self.scope.timezone != self.windows.timezone:
            raise ValueError("inconsistent session reading")
        if instant(self.started_at) > instant(self.completed_at):
            raise ValueError("invalid observation times")
        if self.state == "withheld":
            if not self.reasons or any(v is not None for v in (self.before, self.after, self.difference, self.relative_change_percent)):
                raise ValueError("withheld readings cannot expose counts")
        elif (self.reasons or self.before is None or self.after is None or self.difference != self.after - self.before
              or self.relative_change_percent != relative_percent(self.before, self.after)
              or instant(self.started_at).astimezone(ZoneInfo(self.scope.timezone)).date() <= self.windows.after.end):
            raise ValueError("invalid count arithmetic or eligibility")
        return self


def read_sessions(job: Ga4CollectionJob) -> Ga4SessionReading:
    # Revalidate even mutated instances, including the original qualification and checksums.
    j = Ga4CollectionJob.model_validate_json(job.model_dump_json())
    if j.status != "succeeded":
        raise DeterministicJobError("V22_GA4_SESSION_CONFLICT", "Only saved successful observations can be read.")
    s = checked_ga4_snapshot(j.collection.snapshot)
    reasons: list[Reason] = []
    if not isinstance(s, Ga4ExactSnapshotV2):
        reasons.append("TRUNCATION_NOT_CAPTURED")
    if instant(s.started_at).astimezone(ZoneInfo(s.scope.timezone)).date() <= s.windows.after.end:
        reasons.append("WINDOW_NOT_ELAPSED")
    for p in (s.previous, s.current):
        if len(p.totals.rows) != 1:
            reasons.append("TOTAL_NOT_RETURNED")
        expected = {p.start_date + timedelta(days=i) for i in range((p.end_date-p.start_date).days + 1)}
        if {r.date for r in p.dates.rows} != expected:
            reasons.append("DATES_NOT_RETURNED")
        for v in (p.totals, p.dates):
            m = v.metadata
            for condition, reason in ((bool(m.sampling), "SAMPLED"), (m.subject_to_thresholding, "THRESHOLDED"),
                (m.data_loss_from_other_row, "OTHER_ROW_LOSS"), (bool(m.empty_reason), "EMPTY_REASON"),
                (v.truncated, "TRUNCATED"), (any(r.metric_name == "sessions" for r in m.metric_restrictions), "SESSIONS_RESTRICTED")):
                if condition:
                    reasons.append(reason)
    if isinstance(s, Ga4ExactSnapshotV2) and any(c.reasons for c in s.truncation_checks if c.view in ("totals", "dates")):
        reasons.append("TRUNCATED")
    reasons = list(dict.fromkeys(reasons))
    before = s.previous.totals.rows[0].sessions if not reasons else None
    after = s.current.totals.rows[0].sessions if not reasons else None
    return Ga4SessionReading(schema_version="v22_ga4_session_snapshot_reading_v1", user_id=j.user_id, case_id=j.case_id,
        report_id=j.record.report_id, delivery_revision_id=j.record.delivery_revision_id, collection_id=j.id,
        observation_checksum=verified_request_digest(s.model_dump(mode="json")), implementation=j.selection.implementation,
        action_id=j.selection.action_id, scope=s.scope, windows=s.windows, started_at=s.started_at, completed_at=s.completed_at,
        state="withheld" if reasons else "available", reasons=reasons, before=before, after=after,
        difference=None if reasons else after-before, relative_change_percent=None if reasons else relative_percent(before, after),
        processing_completeness="not_attested", measurement_history="unverified", effect=None, causal_attribution=False)


class Ga4SessionService:
    def __init__(self, repository: Ga4CollectionRepository):
        self.repository = repository

    async def run(self, command: Ga4SessionCommand) -> Ga4SessionReading:
        c = Ga4SessionCommand.model_validate_json(command.model_dump_json())
        job = await self.repository.read_collection(user_id=c.user_id, case_id=c.case_id, collection_id=c.collection_id)
        if job is None or (job.id, job.user_id, job.case_id, job.record.report_id, job.record.delivery_revision_id) != (
            c.collection_id, c.user_id, c.case_id, c.report_id, c.delivery_revision_id):
            raise DeterministicJobError("V22_GA4_SESSION_NOT_FOUND", "This observation is not available.")
        try:
            return read_sessions(job)
        except (ValueError, TypeError, AttributeError):
            raise invalid() from None
