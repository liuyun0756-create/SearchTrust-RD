"""Bounded GA4 aggregate collection for one Case hostname scope."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .gsc import SyncError, bounded_json

GA4_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
DATA_ORIGIN = "https://analyticsdata.googleapis.com/v1beta"
ADMIN_ORIGIN = "https://analyticsadmin.googleapis.com/v1beta"
MAX_SAFE_INTEGER = 2**53 - 1
BASE_LIMITATIONS = [
    "GA4_HOST_FILTERED", "GA4_TOP_ROWS_ONLY", "GA4_BREAKDOWNS_NOT_ADDITIVE",
    "GA4_ESTIMATED_COUNTS", "GA4_PROCESSING_DELAY_2_DAYS",
]
PROPERTY = re.compile(r"^properties/[0-9]+$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Sampling(StrictModel):
    samples_read_count: int = Field(ge=0, le=MAX_SAFE_INTEGER, strict=True)
    sampling_space_size: int = Field(ge=0, le=MAX_SAFE_INTEGER, strict=True)


class MetricRestriction(StrictModel):
    metric_name: str = Field(min_length=1, max_length=128)
    restricted_metric_types: list[Literal[
        "RESTRICTED_METRIC_TYPE_UNSPECIFIED", "COST_DATA", "REVENUE_DATA"
    ]]


class ReportMetadata(StrictModel):
    subject_to_thresholding: bool = False
    data_loss_from_other_row: bool = False
    sampling: list[Sampling] = []
    empty_reason: str | None = Field(default=None, max_length=512)
    time_zone: str = Field(min_length=1, max_length=128)
    metric_restrictions: list[MetricRestriction] = []


class OverviewMetrics(StrictModel):
    sessions: int = Field(ge=0, le=MAX_SAFE_INTEGER, strict=True)
    active_users: int = Field(ge=0, le=MAX_SAFE_INTEGER, strict=True)
    engaged_sessions: int = Field(ge=0, le=MAX_SAFE_INTEGER, strict=True)
    engagement_rate: float = Field(ge=0, le=1, strict=True)
    average_session_duration: float = Field(ge=0, strict=True)
    screen_page_views: int = Field(ge=0, le=MAX_SAFE_INTEGER, strict=True)
    key_events: float = Field(ge=0, strict=True)
    session_key_event_rate: float = Field(ge=0, le=1, strict=True)


class LandingPageRow(StrictModel):
    landing_page: str = Field(min_length=1, max_length=4096)
    sessions: int = Field(ge=0, le=MAX_SAFE_INTEGER, strict=True)
    engaged_sessions: int = Field(ge=0, le=MAX_SAFE_INTEGER, strict=True)
    engagement_rate: float = Field(ge=0, le=1, strict=True)
    average_session_duration: float = Field(ge=0, strict=True)
    screen_page_views: int = Field(ge=0, le=MAX_SAFE_INTEGER, strict=True)
    key_events: float = Field(ge=0, strict=True)
    session_key_event_rate: float = Field(ge=0, le=1, strict=True)


class DateRow(StrictModel):
    date: date
    sessions: int = Field(ge=0, le=MAX_SAFE_INTEGER, strict=True)
    engaged_sessions: int = Field(ge=0, le=MAX_SAFE_INTEGER, strict=True)
    key_events: float = Field(ge=0, strict=True)


class KeyEventRow(StrictModel):
    event_name: str = Field(min_length=1, max_length=256)
    key_events: float = Field(ge=0, strict=True)


class KeyEventConfig(StrictModel):
    event_name: str = Field(min_length=1, max_length=256)
    counting_method: Literal["COUNTING_METHOD_UNSPECIFIED", "ONCE_PER_EVENT", "ONCE_PER_SESSION"]
    custom: bool
    deletable: bool
    create_time: datetime | None = None


class OverviewView(StrictModel):
    rows: list[OverviewMetrics]
    truncated: Literal[False] = False
    metadata: ReportMetadata


class LandingPageView(StrictModel):
    rows: list[LandingPageRow]
    truncated: bool
    metadata: ReportMetadata


class DateView(StrictModel):
    rows: list[DateRow]
    truncated: Literal[False] = False
    metadata: ReportMetadata


class KeyEventView(StrictModel):
    rows: list[KeyEventRow]
    truncated: bool
    metadata: ReportMetadata


class Ga4Period(StrictModel):
    start_date: date
    end_date: date
    totals: OverviewView
    landing_pages: LandingPageView
    dates: DateView
    key_events: KeyEventView


class Ga4Snapshot(StrictModel):
    schema_version: Literal["ga4_sync_v1"] = "ga4_sync_v1"
    resource_id: str
    host_filter: list[str]
    configured_key_events: list[KeyEventConfig]
    current: Ga4Period
    previous: Ga4Period
    limitations: list[str]


REPORTS = {
    "totals": ([], ["sessions", "activeUsers", "engagedSessions", "engagementRate",
        "averageSessionDuration", "screenPageViews", "keyEvents", "sessionKeyEventRate"], 1),
    "landing_pages": (["landingPage"], ["sessions", "engagedSessions", "engagementRate",
        "averageSessionDuration", "screenPageViews", "keyEvents", "sessionKeyEventRate"], 250),
    "dates": (["date"], ["sessions", "engagedSessions", "keyEvents"], 90),
    "key_events": (["eventName"], ["keyEvents"], 100),
}


def _safe_int(value: object) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise ValueError()
    result = int(value)
    if result > MAX_SAFE_INTEGER:
        raise ValueError()
    return result


def _safe_float(value: object) -> float:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError()
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError()
    return result


def _metadata(payload: dict) -> ReportMetadata:
    raw = payload.get("metadata")
    if not isinstance(raw, dict):
        raise ValueError()
    timezone = raw.get("timeZone")
    if not isinstance(timezone, str) or not timezone or len(timezone) > 128 or any(ord(c) < 33 for c in timezone):
        raise ValueError()
    sampling = raw.get("samplingMetadatas", [])
    if not isinstance(sampling, list) or len(sampling) > 2 or not all(isinstance(item, dict) for item in sampling):
        raise ValueError()
    restrictions = raw.get("schemaRestrictionResponse", {}).get("activeMetricRestrictions", [])
    if not isinstance(restrictions, list) or len(restrictions) > 50 or not all(isinstance(item, dict) for item in restrictions):
        raise ValueError()
    normalized_sampling = [Sampling(samples_read_count=_safe_int(item["samplesReadCount"]),
        sampling_space_size=_safe_int(item["samplingSpaceSize"])) for item in sampling]
    if any(item.sampling_space_size == 0 or item.samples_read_count > item.sampling_space_size for item in normalized_sampling):
        raise ValueError()
    return ReportMetadata(
        subject_to_thresholding=raw.get("subjectToThresholding", False),
        data_loss_from_other_row=raw.get("dataLossFromOtherRow", False),
        sampling=normalized_sampling,
        empty_reason=raw.get("emptyReason"), time_zone=timezone,
        metric_restrictions=[MetricRestriction(metric_name=item["metricName"],
            restricted_metric_types=item.get("restrictedMetricTypes", [])) for item in restrictions],
    )


def _headers(payload: dict, dimensions: list[str], metrics: list[str]) -> None:
    dh, mh = payload.get("dimensionHeaders"), payload.get("metricHeaders")
    if not isinstance(dh, list) or not isinstance(mh, list):
        raise ValueError()
    if [item.get("name") if isinstance(item, dict) else None for item in dh] != dimensions:
        raise ValueError()
    if [item.get("name") if isinstance(item, dict) else None for item in mh] != metrics:
        raise ValueError()


def normalize_report(payload: object, *, view: str, start: date, end: date):
    """Reject shape drift rather than persisting ambiguous Google rows."""
    try:
        if view not in REPORTS or not isinstance(payload, dict):
            raise ValueError()
        dimensions, metrics, limit = REPORTS[view]
        _headers(payload, dimensions, metrics)
        metadata = _metadata(payload)
        raw_rows = payload.get("rows", [])
        if not isinstance(raw_rows, list) or len(raw_rows) > limit + 1:
            raise ValueError()
        row_count = payload.get("rowCount", 0)
        if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < len(raw_rows):
            raise ValueError()
        rows, seen = [], set()
        for raw in raw_rows:
            if not isinstance(raw, dict):
                raise ValueError()
            dims, vals = raw.get("dimensionValues", []), raw.get("metricValues")
            if not isinstance(dims, list) or not isinstance(vals, list) or len(dims) != len(dimensions) or len(vals) != len(metrics):
                raise ValueError()
            keys = [item.get("value") if isinstance(item, dict) else None for item in dims]
            values = [item.get("value") if isinstance(item, dict) else None for item in vals]
            if any(not isinstance(key, str) or not key or len(key) > 4096 or any(ord(c) < 32 for c in key) for key in keys) or tuple(keys) in seen:
                raise ValueError()
            seen.add(tuple(keys))
            data = dict(zip(metrics, values))
            if view == "totals":
                rows.append(OverviewMetrics(sessions=_safe_int(data["sessions"]), active_users=_safe_int(data["activeUsers"]),
                    engaged_sessions=_safe_int(data["engagedSessions"]), engagement_rate=_safe_float(data["engagementRate"]),
                    average_session_duration=_safe_float(data["averageSessionDuration"]), screen_page_views=_safe_int(data["screenPageViews"]),
                    key_events=_safe_float(data["keyEvents"]), session_key_event_rate=_safe_float(data["sessionKeyEventRate"])))
            elif view == "landing_pages":
                rows.append(LandingPageRow(landing_page=keys[0], sessions=_safe_int(data["sessions"]),
                    engaged_sessions=_safe_int(data["engagedSessions"]), engagement_rate=_safe_float(data["engagementRate"]),
                    average_session_duration=_safe_float(data["averageSessionDuration"]), screen_page_views=_safe_int(data["screenPageViews"]),
                    key_events=_safe_float(data["keyEvents"]), session_key_event_rate=_safe_float(data["sessionKeyEventRate"])))
            elif view == "dates":
                parsed = datetime.strptime(keys[0], "%Y%m%d").date()
                if parsed.strftime("%Y%m%d") != keys[0] or not start <= parsed <= end:
                    raise ValueError()
                rows.append(DateRow(date=parsed, sessions=_safe_int(data["sessions"]),
                    engaged_sessions=_safe_int(data["engagedSessions"]), key_events=_safe_float(data["keyEvents"])))
            else:
                rows.append(KeyEventRow(event_name=keys[0], key_events=_safe_float(data["keyEvents"])))
        truncated = len(rows) > limit or row_count > limit
        rows = rows[:limit]
        if view == "totals":
            if len(rows) > 1:
                raise ValueError()
            return OverviewView(rows=rows, metadata=metadata)
        if view == "landing_pages":
            return LandingPageView(rows=rows, truncated=truncated, metadata=metadata)
        if view == "dates":
            if truncated:
                raise ValueError()
            return DateView(rows=rows, metadata=metadata)
        return KeyEventView(rows=rows, truncated=truncated, metadata=metadata)
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError, OverflowError):
        raise SyncError("SYNC_INVALID_GOOGLE_RESPONSE") from None


def _classify(status: int, payload: object) -> None:
    if status == 200:
        return
    if status == 429 or status >= 500:
        raise SyncError("SYNC_GOOGLE_UNAVAILABLE", True)
    if status == 401:
        raise SyncError("SYNC_GOOGLE_TOKEN_EXPIRED", True)
    if status in (403, 404):
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        errors = error.get("errors", []) if isinstance(error, dict) else []
        if isinstance(errors, list) and any(isinstance(item, dict) and item.get("reason") in
            ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "RESOURCE_EXHAUSTED") for item in errors):
            raise SyncError("SYNC_GOOGLE_UNAVAILABLE", True)
        raise SyncError("SYNC_GOOGLE_ACCESS_DENIED")
    raise SyncError("SYNC_GOOGLE_REJECTED")


class Ga4Provider:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def _configured_events(self, resource_id: str, token: str) -> list[KeyEventConfig]:
        result, page_token, seen = [], "", set()
        for _ in range(20):
            status, payload = await bounded_json(self.client, "GET", f"{ADMIN_ORIGIN}/{resource_id}/keyEvents",
                headers={"authorization": f"Bearer {token}"}, params={"pageSize": "200", "pageToken": page_token})
            _classify(status, payload)
            try:
                if not isinstance(payload, dict) or not isinstance(payload.get("keyEvents", []), list) or len(payload.get("keyEvents", [])) > 200:
                    raise ValueError()
                for item in payload.get("keyEvents", []):
                    if not isinstance(item, dict) or item.get("eventName") in {event.event_name for event in result}:
                        raise ValueError()
                    name, event_name = item.get("name"), item.get("eventName")
                    if not isinstance(name, str) or not name.startswith(f"{resource_id}/keyEvents/") or "/" in name.removeprefix(f"{resource_id}/keyEvents/") or not name.removeprefix(f"{resource_id}/keyEvents/"):
                        raise ValueError()
                    if not isinstance(event_name, str) or any(ord(c) < 32 for c in event_name):
                        raise ValueError()
                    created = item.get("createTime")
                    parsed_created = datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None
                    if parsed_created is not None and parsed_created.tzinfo is None:
                        raise ValueError()
                    result.append(KeyEventConfig(event_name=event_name,
                        counting_method=item.get("countingMethod", "COUNTING_METHOD_UNSPECIFIED"),
                        custom=item.get("custom", False), deletable=item.get("deletable", False), create_time=parsed_created))
                next_token = payload.get("nextPageToken", "")
                if not isinstance(next_token, str) or len(next_token) > 4096 or any(ord(c) < 33 for c in next_token):
                    raise ValueError()
                if not next_token:
                    return sorted(result, key=lambda event: event.event_name)
                if next_token in seen:
                    raise ValueError()
                seen.add(next_token)
                page_token = next_token
            except (ValueError, TypeError, KeyError, ValidationError, AttributeError):
                raise SyncError("SYNC_INVALID_GOOGLE_RESPONSE") from None
        raise SyncError("SYNC_INVALID_GOOGLE_RESPONSE")

    @staticmethod
    def _filter(hosts: list[str]) -> dict:
        return {"andGroup": {"expressions": [
            {"filter": {"fieldName": "platform", "stringFilter": {"matchType": "EXACT", "value": "web", "caseSensitive": False}}},
            {"filter": {"fieldName": "hostName", "inListFilter": {"values": hosts, "caseSensitive": False}}},
        ]}}

    @staticmethod
    def _valid_host(host: object) -> bool:
        if not isinstance(host, str) or len(host) > 253 or host != host.lower() or not re.fullmatch(r"[a-z0-9.-]+", host):
            return False
        labels = host.split(".")
        return len(labels) >= 2 and all(1 <= len(label) <= 63 and not label.startswith("-") and not label.endswith("-") for label in labels)

    async def collect(self, resource_id: str, hosts: list[str], token: str, end: date) -> Ga4Snapshot:
        if not PROPERTY.fullmatch(resource_id) or not 1 <= len(hosts) <= 2 or len(set(hosts)) != len(hosts) or any(
            not self._valid_host(host) for host in hosts):
            raise SyncError("SYNC_INVALID_RESOURCE")
        configured = await self._configured_events(resource_id, token)
        periods = []
        for last in (end, end - timedelta(days=90)):
            first, views = last - timedelta(days=89), {}
            for name, (dimensions, metrics, limit) in REPORTS.items():
                request_limit = limit + 1 if name in ("landing_pages", "key_events") else limit
                body = {"dateRanges": [{"startDate": first.isoformat(), "endDate": last.isoformat()}],
                    "dimensions": [{"name": item} for item in dimensions], "metrics": [{"name": item} for item in metrics],
                    "dimensionFilter": self._filter(hosts), "limit": str(request_limit), "offset": "0", "keepEmptyRows": False}
                if name == "landing_pages":
                    body["orderBys"] = [{"metric": {"metricName": "sessions"}, "desc": True}]
                elif name == "dates":
                    body["orderBys"] = [{"dimension": {"dimensionName": "date"}, "desc": False}]
                if name == "key_events":
                    body["orderBys"] = [{"metric": {"metricName": "keyEvents"}, "desc": True}]
                    body["metricFilter"] = {"filter": {"fieldName": "keyEvents", "numericFilter": {
                        "operation": "GREATER_THAN", "value": {"int64Value": "0"}}}}
                status, payload = await bounded_json(self.client, "POST", f"{DATA_ORIGIN}/{resource_id}:runReport",
                    headers={"authorization": f"Bearer {token}", "content-type": "application/json"}, body=body)
                _classify(status, payload)
                views[name] = normalize_report(payload, view=name, start=first, end=last)
            periods.append(Ga4Period(start_date=first, end_date=last, **views))
        timezones = {getattr(period, view).metadata.time_zone for period in periods for view in REPORTS}
        if len(timezones) != 1:
            raise SyncError("SYNC_INVALID_GOOGLE_RESPONSE")
        limitations = list(BASE_LIMITATIONS)
        if any(period.landing_pages.truncated for period in periods):
            limitations.append("GA4_LANDING_PAGE_TRUNCATED")
        if any(period.key_events.truncated for period in periods):
            limitations.append("GA4_KEY_EVENT_TRUNCATED")
        return Ga4Snapshot(resource_id=resource_id, host_filter=hosts, configured_key_events=configured,
            current=periods[0], previous=periods[1], limitations=limitations)


def evaluate_health(snapshot: Ga4Snapshot) -> tuple[str, list[str]]:
    current, problems, warnings = snapshot.current, [], []
    totals = current.totals.rows[0] if current.totals.rows else None
    if totals is None or totals.sessions == 0:
        problems.append("GA4_NO_CURRENT_SESSIONS")
    if not current.landing_pages.rows:
        problems.append("GA4_NO_LANDING_PAGE_ROWS")
    if not current.dates.rows:
        problems.append("GA4_NO_ACTIVITY_DATES")
    if not snapshot.configured_key_events:
        problems.append("GA4_NO_CONFIGURED_KEY_EVENTS")
    active_dates = sorted(row.date for row in current.dates.rows if row.sessions > 0)
    if active_dates:
        if (current.end_date - active_dates[-1]).days > 7:
            problems.append("GA4_RECENT_ACTIVITY_MISSING_REVIEW")
        if any((right - left).days - 1 >= 14 for left, right in zip(active_dates, active_dates[1:])):
            problems.append("GA4_ACTIVITY_GAP_REVIEW")
    if snapshot.configured_key_events and (totals is None or totals.key_events == 0):
        warnings.append("GA4_CONFIGURED_KEY_EVENTS_NO_ACTIVITY")
    if totals is not None and totals.engaged_sessions == 0:
        warnings.append("GA4_NO_ENGAGED_SESSIONS_REVIEW")
    previous = snapshot.previous.totals.rows[0] if snapshot.previous.totals.rows else None
    if previous is None or previous.sessions == 0:
        warnings.append("GA4_COMPARISON_UNAVAILABLE")
    metadata = [getattr(period, view).metadata for period in (snapshot.current, snapshot.previous) for view in REPORTS]
    if any(item.sampling for item in metadata): warnings.append("GA4_SAMPLED_DATA")
    if any(item.subject_to_thresholding for item in metadata): warnings.append("GA4_THRESHOLDING_APPLIED")
    if any(item.data_loss_from_other_row for item in metadata): warnings.append("GA4_OTHER_ROW_DATA_LOSS")
    if any(item.metric_restrictions for item in metadata): warnings.append("GA4_METRIC_RESTRICTIONS")
    if any(row.landing_page == "(not set)" for period in (snapshot.current, snapshot.previous) for row in period.landing_pages.rows):
        warnings.append("GA4_LANDING_PAGE_NOT_SET")
    return ("unhealthy" if problems else "healthy"), list(dict.fromkeys(problems + snapshot.limitations + warnings))
