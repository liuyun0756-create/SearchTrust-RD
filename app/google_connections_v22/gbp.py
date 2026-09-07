"""Bounded, read-only GBP collection with a 30-day Content boundary."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal
from urllib.parse import urlsplit
import re

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .gsc import SyncError, bounded_json

GBP_SCOPE = "https://www.googleapis.com/auth/business.manage"
INFO_ORIGIN = "https://mybusinessbusinessinformation.googleapis.com/v1"
PERFORMANCE_ORIGIN = "https://businessprofileperformance.googleapis.com/v1"
MAX_SAFE_INTEGER = 2**53 - 1
LOCATION = re.compile(r"^locations/[0-9]+$")
READ_MASK = (
    "name,title,phoneNumbers,categories,storefrontAddress,websiteUri,regularHours,"
    "specialHours,serviceArea,openInfo,metadata,profile,moreHours,serviceItems"
)
DAILY_METRICS = (
    "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH",
    "BUSINESS_IMPRESSIONS_MOBILE_SEARCH",
    "BUSINESS_IMPRESSIONS_DESKTOP_MAPS",
    "BUSINESS_IMPRESSIONS_MOBILE_MAPS",
    "CALL_CLICKS",
    "BUSINESS_DIRECTION_REQUESTS",
    "WEBSITE_CLICKS",
)
IMPRESSION_METRICS = frozenset(DAILY_METRICS[:4])
BASE_LIMITATIONS = ("GBP_READ_ONLY", "GBP_CONTENT_TTL_30D", "GBP_KEYWORDS_MONTHLY")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ProfileChecks(StrictModel):
    voice_of_merchant: bool
    open: bool
    title: bool
    website: bool
    phone: bool
    primary_category: bool
    regular_hours: bool
    address_or_service_area: bool


class PerformancePeriod(StrictModel):
    start_date: date
    end_date: date
    impressions: int = Field(ge=0, le=MAX_SAFE_INTEGER, strict=True)


class KeywordRow(StrictModel):
    keyword: str = Field(min_length=1, max_length=500)
    value: int | None = Field(default=None, ge=0, le=MAX_SAFE_INTEGER)
    threshold: int | None = Field(default=None, ge=0, le=MAX_SAFE_INTEGER)

    @model_validator(mode="after")
    def exact_or_threshold(self):
        if (self.value is None) == (self.threshold is None):
            raise ValueError("exactly one keyword insight value is required")
        return self


class KeywordSummary(StrictModel):
    start_month: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    end_month: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    pages: int = Field(ge=1, le=10, strict=True)
    available: bool
    threshold_applied: bool
    truncated: bool


class GbpSnapshot(StrictModel):
    schema_version: Literal["gbp_sync_v1"] = "gbp_sync_v1"
    resource_id: str = Field(pattern=r"^locations/[0-9]+$")
    profile_checks: ProfileChecks
    current: PerformancePeriod
    previous: PerformancePeriod
    keywords: KeywordSummary
    keyword_rows: list[KeywordRow] = Field(max_length=1000)
    limitations: list[str]


@dataclass(frozen=True)
class GbpCollection:
    snapshot: GbpSnapshot
    raw_payload: dict

    def manifest(self) -> dict:
        """Return only durable, non-Content facts and boolean conclusions."""
        value = self.snapshot
        return {
            "schema_version": value.schema_version,
            "resource_id": value.resource_id,
            "current": {
                "start_date": value.current.start_date.isoformat(),
                "end_date": value.current.end_date.isoformat(),
                "has_impressions": value.current.impressions > 0,
            },
            "previous": {
                "start_date": value.previous.start_date.isoformat(),
                "end_date": value.previous.end_date.isoformat(),
                "has_impressions": value.previous.impressions > 0,
            },
            "keywords": value.keywords.model_dump(mode="json"),
            "profile_checks": value.profile_checks.model_dump(mode="json"),
            "limitations": value.limitations,
        }


def _row(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError()
    return value


def _rows(value: object, maximum: int) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError()
    return [_row(item) for item in value]


def _text(value: object, maximum: int = 4096) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise ValueError()
    return value


def _boolean(value: object) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError()
    return value


def _safe_int(value: object) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 32:
        raise ValueError()
    result = int(value)
    if result > MAX_SAFE_INTEGER:
        raise ValueError()
    return result


def _date(value: object, start: date, end: date) -> date:
    item = _row(value)
    if set(item) != {"year", "month", "day"} or any(
        not isinstance(item[key], int) or isinstance(item[key], bool) for key in item
    ):
        raise ValueError()
    parsed = date(item["year"], item["month"], item["day"])
    if not start <= parsed <= end:
        raise ValueError()
    return parsed


def _valid_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.netloc)
        and not parsed.username
        and not parsed.password
        and len(value) <= 2083
    )


def normalize_location(payload: object, resource_id: str) -> ProfileChecks:
    try:
        item = _row(payload)
        allowed = set(READ_MASK.split(","))
        if set(item) - allowed or item.get("name") != resource_id:
            raise ValueError()
        title = _text(item.get("title"), 240)
        website = _text(item.get("websiteUri"), 2083)
        if website is not None and not _valid_url(website):
            raise ValueError()
        phones = _row(item.get("phoneNumbers", {}))
        primary_phone = _text(phones.get("primaryPhone"), 120)
        categories = _row(item.get("categories", {}))
        primary_category = _row(categories.get("primaryCategory", {}))
        category_name = _text(primary_category.get("name"), 240)
        hours = _row(item.get("regularHours", {}))
        periods = _rows(hours.get("periods", []), 100)
        address = _row(item.get("storefrontAddress", {}))
        service_area = _row(item.get("serviceArea", {}))
        open_info = _row(item.get("openInfo", {}))
        status = _text(open_info.get("status"), 64)
        if status is not None and status not in {
            "OPEN_FOR_BUSINESS_UNSPECIFIED", "OPEN", "CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"
        }:
            raise ValueError()
        metadata = _row(item.get("metadata", {}))
        voice = _boolean(metadata.get("hasVoiceOfMerchant"))
        # Validate requested top-level collection shapes even when health does not use their values.
        _row(item.get("specialHours", {}))
        _row(item.get("profile", {}))
        _rows(item.get("moreHours", []), 100)
        _rows(item.get("serviceItems", []), 1000)
        address_present = any(value not in (None, "", []) for value in address.values())
        service_area_present = any(value not in (None, "", [], {}) for value in service_area.values())
        return ProfileChecks(
            voice_of_merchant=voice is True,
            open=status == "OPEN",
            title=bool(title),
            website=bool(website),
            phone=bool(primary_phone),
            primary_category=bool(category_name),
            regular_hours=bool(periods),
            address_or_service_area=address_present or service_area_present,
        )
    except (ValueError, TypeError, KeyError, ValidationError, OverflowError):
        raise SyncError("SYNC_INVALID_GOOGLE_RESPONSE") from None


def normalize_performance(payload: object, start: date, end: date) -> dict[str, dict[date, int]]:
    try:
        item = _row(payload)
        if set(item) - {"multiDailyMetricTimeSeries"}:
            raise ValueError()
        groups = _rows(item.get("multiDailyMetricTimeSeries", []), len(DAILY_METRICS))
        result: dict[str, dict[date, int]] = {}
        for group in groups:
            if set(group) - {"dailyMetricTimeSeries"}:
                raise ValueError()
            for series in _rows(group.get("dailyMetricTimeSeries", []), len(DAILY_METRICS)):
                if set(series) - {"dailyMetric", "dailySubEntityType", "timeSeries"}:
                    raise ValueError()
                metric = _text(series.get("dailyMetric"), 128)
                if metric not in DAILY_METRICS or metric in result or series.get("dailySubEntityType") not in (None, {}):
                    raise ValueError()
                time_series = _row(series.get("timeSeries", {}))
                if set(time_series) - {"datedValues"}:
                    raise ValueError()
                values: dict[date, int] = {}
                for point in _rows(time_series.get("datedValues", []), 180):
                    if set(point) - {"date", "value"}:
                        raise ValueError()
                    day = _date(point.get("date"), start, end)
                    if day in values:
                        raise ValueError()
                    values[day] = 0 if "value" not in point else _safe_int(point["value"])
                result[metric] = values
        return result
    except (ValueError, TypeError, KeyError, ValidationError, OverflowError):
        raise SyncError("SYNC_INVALID_GOOGLE_RESPONSE") from None


def normalize_keyword_page(payload: object, seen_keywords: set[str]) -> tuple[list[KeywordRow], str]:
    try:
        item = _row(payload)
        if set(item) - {"searchKeywordsCounts", "nextPageToken"}:
            raise ValueError()
        rows = []
        for raw in _rows(item.get("searchKeywordsCounts", []), 100):
            if set(raw) != {"searchKeyword", "insightsValue"}:
                raise ValueError()
            keyword = _text(raw["searchKeyword"], 500)
            normalized = keyword.casefold()
            if normalized in seen_keywords:
                raise ValueError()
            seen_keywords.add(normalized)
            insight = _row(raw["insightsValue"])
            if set(insight) == {"value"}:
                rows.append(KeywordRow(keyword=keyword, value=_safe_int(insight["value"])))
            elif set(insight) == {"threshold"}:
                rows.append(KeywordRow(keyword=keyword, threshold=_safe_int(insight["threshold"])))
            else:
                raise ValueError()
        token = _text(item.get("nextPageToken"), 4096) or ""
        return rows, token
    except (ValueError, TypeError, KeyError, ValidationError, OverflowError):
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
        if isinstance(errors, list) and any(
            isinstance(value, dict) and value.get("reason") in
            ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "RESOURCE_EXHAUSTED")
            for value in errors
        ):
            raise SyncError("SYNC_GOOGLE_UNAVAILABLE", True)
        raise SyncError("SYNC_GOOGLE_ACCESS_DENIED")
    raise SyncError("SYNC_GOOGLE_REJECTED")


class GbpProvider:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def _get(self, url: str, token: str, params: object) -> object:
        status, payload = await bounded_json(
            self.client, "GET", url, headers={"authorization": f"Bearer {token}"}, params=params  # type: ignore[arg-type]
        )
        _classify(status, payload)
        return payload

    async def collect(self, resource_id: str, token: str, end: date) -> GbpCollection:
        if not LOCATION.fullmatch(resource_id) or not isinstance(token, str) or not token:
            raise SyncError("SYNC_INVALID_RESOURCE")
        start = end - timedelta(days=179)
        location_payload = await self._get(
            f"{INFO_ORIGIN}/{resource_id}", token, {"readMask": READ_MASK}
        )
        profile_checks = normalize_location(location_payload, resource_id)

        performance_params = [("dailyMetrics", metric) for metric in DAILY_METRICS]
        for prefix, value in (("start", start), ("end", end)):
            performance_params.extend([
                (f"dailyRange.{prefix}_date.year", str(value.year)),
                (f"dailyRange.{prefix}_date.month", str(value.month)),
                (f"dailyRange.{prefix}_date.day", str(value.day)),
            ])
        performance_payload = await self._get(
            f"{PERFORMANCE_ORIGIN}/{resource_id}:fetchMultiDailyMetricsTimeSeries",
            token,
            performance_params,
        )
        performance = normalize_performance(performance_payload, start, end)

        keyword_start = start.replace(day=1)
        keyword_rows: list[KeywordRow] = []
        keyword_pages: list[object] = []
        seen_keywords: set[str] = set()
        seen_tokens: set[str] = set()
        page_token = ""
        truncated = False
        for page_number in range(1, 11):
            params = {
                "monthlyRange.start_month.year": str(keyword_start.year),
                "monthlyRange.start_month.month": str(keyword_start.month),
                "monthlyRange.end_month.year": str(end.year),
                "monthlyRange.end_month.month": str(end.month),
                "pageSize": "100",
                "pageToken": page_token,
            }
            payload = await self._get(
                f"{PERFORMANCE_ORIGIN}/{resource_id}/searchkeywords/impressions/monthly", token, params
            )
            page_rows, next_token = normalize_keyword_page(payload, seen_keywords)
            keyword_rows.extend(page_rows)
            keyword_pages.append(payload)
            if not next_token:
                break
            if next_token in seen_tokens:
                raise SyncError("SYNC_INVALID_GOOGLE_RESPONSE")
            seen_tokens.add(next_token)
            if page_number == 10:
                truncated = True
                break
            page_token = next_token

        def impressions(first: date, last: date) -> int:
            total = sum(
                value for metric in IMPRESSION_METRICS
                for day, value in performance.get(metric, {}).items() if first <= day <= last
            )
            if total > MAX_SAFE_INTEGER:
                raise SyncError("SYNC_INVALID_GOOGLE_RESPONSE")
            return total

        current_start = end - timedelta(days=89)
        previous_end = end - timedelta(days=90)
        limitations = list(BASE_LIMITATIONS)
        if not keyword_rows:
            limitations.append("GBP_KEYWORDS_UNAVAILABLE")
        if any(row.threshold is not None for row in keyword_rows):
            limitations.append("GBP_KEYWORDS_THRESHOLD_APPLIED")
        if truncated:
            limitations.append("GBP_KEYWORDS_TRUNCATED")
        snapshot = GbpSnapshot(
            resource_id=resource_id,
            profile_checks=profile_checks,
            current=PerformancePeriod(start_date=current_start, end_date=end,
                impressions=impressions(current_start, end)),
            previous=PerformancePeriod(start_date=start, end_date=previous_end,
                impressions=impressions(start, previous_end)),
            keywords=KeywordSummary(
                start_month=keyword_start.strftime("%Y-%m"), end_month=end.strftime("%Y-%m"),
                pages=len(keyword_pages), available=bool(keyword_rows),
                threshold_applied=any(row.threshold is not None for row in keyword_rows), truncated=truncated,
            ),
            keyword_rows=keyword_rows,
            limitations=limitations,
        )
        return GbpCollection(snapshot=snapshot, raw_payload={
            "business_information": location_payload,
            "performance": performance_payload,
            "keyword_pages": keyword_pages,
        })


def evaluate_health(snapshot: GbpSnapshot) -> tuple[str, list[str]]:
    checks = snapshot.profile_checks
    problems = []
    for present, code in (
        (checks.voice_of_merchant, "GBP_LOCATION_UNVERIFIED"),
        (checks.open, "GBP_LOCATION_NOT_OPEN"),
        (checks.title, "GBP_TITLE_MISSING"),
        (checks.website, "GBP_WEBSITE_MISSING"),
        (checks.phone, "GBP_PHONE_MISSING"),
        (checks.primary_category, "GBP_PRIMARY_CATEGORY_MISSING"),
        (checks.regular_hours, "GBP_REGULAR_HOURS_MISSING"),
        (checks.address_or_service_area, "GBP_ADDRESS_AND_SERVICE_AREA_MISSING"),
        (snapshot.current.impressions > 0, "GBP_NO_CURRENT_IMPRESSIONS"),
    ):
        if not present:
            problems.append(code)
    warnings = list(snapshot.limitations)
    if snapshot.previous.impressions == 0:
        warnings.append("GBP_COMPARISON_UNAVAILABLE")
    if not snapshot.keywords.available:
        warnings.append("GBP_KEYWORDS_UNAVAILABLE")
    return ("unhealthy" if problems else "healthy"), list(dict.fromkeys(problems + warnings))
