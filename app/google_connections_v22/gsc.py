"""Bounded, read-only GSC collection. No token or raw provider error persistence."""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

GSC_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
VIEWS = {"totals": (None, 1), "queries": ("query", 250), "pages": ("page", 250),
         "dates": ("date", 90), "devices": ("device", 3), "countries": ("country", 250)}
LIMITATIONS = ["GSC_TOP_ROWS_ONLY", "GSC_QUERY_PRIVACY_FILTERING", "GSC_WEB_ONLY",
              "GSC_SELECTED_PROPERTY_SCOPE", "GSC_BREAKDOWNS_NOT_ADDITIVE"]


class SyncError(Exception):
    def __init__(self, code: str, retryable: bool = False):
        super().__init__(code)
        self.code, self.retryable = code, retryable


async def bounded_json(client: httpx.AsyncClient, method: str, url: str, *, headers: dict, body: dict | None = None,
                       content: str | None = None, params: dict | None = None) -> tuple[int, object]:
    try:
        async with client.stream(method, url, headers=headers, json=body, content=content, params=params,
                                 follow_redirects=False, timeout=20) as response:
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 2_000_000:
                    raise SyncError("SYNC_RESPONSE_TOO_LARGE")
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError):
                payload = None
            return response.status_code, payload
    except (httpx.HTTPError, OSError):
        raise SyncError("SYNC_NETWORK_UNAVAILABLE", True) from None


class MetricRow(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    key: str | None = Field(default=None, max_length=4096)
    clicks: float = Field(ge=0, strict=True)
    impressions: float = Field(ge=0, strict=True)
    ctr: float = Field(ge=0, le=1, strict=True)
    position: float = Field(ge=0, strict=True)


class View(BaseModel):
    rows: list[MetricRow]
    truncated: bool
    aggregation_type: Literal["auto", "byProperty", "byPage"]


class Period(BaseModel):
    start_date: date
    end_date: date
    totals: View
    queries: View
    pages: View
    dates: View
    devices: View
    countries: View


class GscSnapshot(BaseModel):
    schema_version: Literal["gsc_sync_v1"] = "gsc_sync_v1"
    resource_id: str
    search_type: Literal["web"] = "web"
    data_state: Literal["final"] = "final"
    timezone: Literal["America/Los_Angeles"] = "America/Los_Angeles"
    current: Period
    previous: Period
    limitations: list[str]


def normalize_view(payload: object, *, dimension: str | None, limit: int, start: date, end: date) -> View:
    try:
        if not isinstance(payload, dict) or not isinstance(payload.get("rows", []), list):
            raise ValueError()
        rows = payload.get("rows", [])
        if len(rows) > limit + 1:
            raise ValueError()
        normalized = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError()
            keys = row.get("keys", [])
            if not isinstance(keys, list) or len(keys) != (1 if dimension else 0):
                raise ValueError()
            key = keys[0] if dimension else None
            if dimension and (not isinstance(key, str) or not key):
                raise ValueError()
            if key in seen:
                raise ValueError()
            seen.add(key)
            if dimension == "date" and (date.fromisoformat(key).isoformat() != key or not start <= date.fromisoformat(key) <= end):
                raise ValueError()
            if dimension == "device" and key.upper() not in ("DESKTOP", "MOBILE", "TABLET"):
                raise ValueError()
            if dimension == "country" and (len(key) != 3 or not key.isascii() or not key.isalpha()):
                raise ValueError()
            normalized.append(MetricRow(key=key, **{k: row[k] for k in ("clicks", "impressions", "ctr", "position")}))
        # Dates, device and ungrouped totals have fixed cardinalities, not top-row truncation.
        if dimension in (None, "date", "device") and len(normalized) > limit:
            raise ValueError()
        return View(rows=normalized[:limit], truncated=len(normalized) > limit,
                    aggregation_type=payload.get("responseAggregationType", "auto"))
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError):
        raise SyncError("SYNC_INVALID_GOOGLE_RESPONSE") from None


class GscProvider:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def collect(self, resource_id: str, token: str, end: date) -> GscSnapshot:
        if not resource_id or len(resource_id) > 2048:
            raise SyncError("SYNC_INVALID_RESOURCE")
        periods = []
        for last in (end, end - timedelta(days=90)):
            first = last - timedelta(days=89)
            views = {}
            for name, (dimension, limit) in VIEWS.items():
                status, payload = await bounded_json(self.client, "POST",
                    f"https://www.googleapis.com/webmasters/v3/sites/{quote(resource_id, safe='')}/searchAnalytics/query",
                    headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
                    body={"startDate": first.isoformat(), "endDate": last.isoformat(), "type": "web", "dataState": "final",
                          "dimensions": [dimension] if dimension else [], "rowLimit": limit + 1, "startRow": 0,
                          "aggregationType": "auto" if dimension == "page" else "byProperty"})
                if status == 429 or status >= 500:
                    raise SyncError("SYNC_GOOGLE_UNAVAILABLE", True)
                if status == 401:
                    raise SyncError("SYNC_GOOGLE_TOKEN_EXPIRED", True)
                if status in (403, 404):
                    error = payload.get("error", {}) if isinstance(payload, dict) else {}
                    errors = error.get("errors", []) if isinstance(error, dict) else []
                    if isinstance(errors, list) and any(isinstance(e, dict) and e.get("reason") in
                        ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded") for e in errors):
                        raise SyncError("SYNC_GOOGLE_UNAVAILABLE", True)
                    raise SyncError("SYNC_GOOGLE_ACCESS_DENIED")
                if status != 200:
                    raise SyncError("SYNC_GOOGLE_REJECTED")
                views[name] = normalize_view(payload, dimension=dimension, limit=limit, start=first, end=last)
            periods.append(Period(start_date=first, end_date=last, **views))
        limitations = list(LIMITATIONS)
        if any(getattr(period, view).truncated for period in periods for view in VIEWS):
            limitations.append("GSC_DETAIL_TRUNCATED")
        return GscSnapshot(resource_id=resource_id, current=periods[0], previous=periods[1], limitations=limitations)


def evaluate_health(snapshot: GscSnapshot) -> tuple[str, list[str]]:
    current = snapshot.current
    problems = []
    if not current.totals.rows or current.totals.rows[0].impressions <= 0:
        problems.append("GSC_NO_CURRENT_DATA")
    if not current.queries.rows:
        problems.append("GSC_NO_QUERY_ROWS")
    if not current.pages.rows:
        problems.append("GSC_NO_PAGE_ROWS")
    dates = sorted(date.fromisoformat(row.key) for row in current.dates.rows if row.impressions > 0 and row.key)
    if not dates:
        problems.append("GSC_NO_ACTIVITY_DATES")
    else:
        if (current.end_date - dates[-1]).days > 7:
            problems.append("GSC_RECENT_ACTIVITY_MISSING_REVIEW")
        if any((right - left).days - 1 >= 14 for left, right in zip(dates, dates[1:])):
            problems.append("GSC_ACTIVITY_GAP_REVIEW")
    reasons = problems + snapshot.limitations
    if not snapshot.previous.totals.rows or snapshot.previous.totals.rows[0].impressions <= 0:
        reasons.append("GSC_COMPARISON_UNAVAILABLE")
    return ("unhealthy" if problems else "healthy"), reasons
