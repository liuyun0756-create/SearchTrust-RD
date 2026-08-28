"""Strict internal contracts for the bounded v2.2 SERP market collector."""

from __future__ import annotations

from collections import Counter
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from app.report_v22.models import StrictModel


SERP_QUERY_MIN = 3
SERP_QUERY_MAX = 5
SERP_CALLS_PER_QUERY = 2
SERP_LOGICAL_CALL_LIMIT = 10
SERP_PROVIDER_ATTEMPT_LIMIT_PER_CALL = 3
SERP_RESULT_LIMIT_PER_TYPE = 20
SERP_MAP_ZOOM = 14

SerpEngine = Literal["google_maps", "google"]
SerpResultType = Literal["maps", "local_pack", "organic"]
SerpCallStatus = Literal[
    "succeeded",
    "failed_transient",
    "failed_deterministic",
]
SerpRankSource = Literal["provider_position", "response_order"]
SerpTargetPointSource = Literal["explicit_coordinates", "serpapi_location"]
SearchDevice = Literal["desktop", "mobile"]
SerpLimitation = Annotated[str, Field(min_length=1, max_length=300)]
SerpCategory = Annotated[str, Field(min_length=1, max_length=240)]


class SerpTargetPoint(StrictModel):
    requested_label: str = Field(min_length=1, max_length=200)
    canonical_name: str = Field(min_length=1, max_length=300)
    country_code: str = Field(min_length=2, max_length=2, pattern=r"^[A-Z]{2}$")
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    source: SerpTargetPointSource
    provider_location_id: str | None = Field(default=None, max_length=200)
    resolved_at: AwareDatetime
    response_checksum: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )

    @model_validator(mode="after")
    def validate_source_audit(self) -> "SerpTargetPoint":
        if self.source == "serpapi_location":
            if not self.provider_location_id or not self.response_checksum:
                raise ValueError("provider locations require id and response checksum")
        elif self.provider_location_id is not None or self.response_checksum is not None:
            raise ValueError("explicit coordinates cannot include provider location audit")
        return self


class SerpCallRecord(StrictModel):
    call_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    request_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    query: str = Field(min_length=1, max_length=300)
    engine: SerpEngine
    result_types: list[SerpResultType] = Field(min_length=1, max_length=2)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    country_code: str = Field(min_length=2, max_length=2, pattern=r"^[A-Z]{2}$")
    language: str = Field(min_length=2, max_length=12, pattern=r"^[A-Za-z0-9-]+$")
    device: SearchDevice
    maps_zoom: int | None = Field(default=None, ge=3, le=30)
    status: SerpCallStatus
    started_at: AwareDatetime
    completed_at: AwareDatetime
    provider_search_id: str | None = Field(default=None, max_length=200)
    response_checksum: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    logical_call_used: bool
    provider_attempts: int = Field(ge=0, le=SERP_PROVIDER_ATTEMPT_LIMIT_PER_CALL)
    checkpoint_hit: bool = False
    error_code: str | None = Field(
        default=None,
        max_length=120,
        pattern=r"^[A-Z0-9_]+$",
    )

    @model_validator(mode="after")
    def validate_call(self) -> "SerpCallRecord":
        if self.completed_at < self.started_at:
            raise ValueError("call completion cannot precede start")
        expected_types = ["maps"] if self.engine == "google_maps" else ["local_pack", "organic"]
        if self.result_types != expected_types:
            raise ValueError("result types must match engine")
        if self.engine == "google_maps" and self.maps_zoom != SERP_MAP_ZOOM:
            raise ValueError("maps calls require the fixed map zoom")
        if self.engine == "google" and self.maps_zoom is not None:
            raise ValueError("google search calls cannot include map zoom")
        if self.logical_call_used != (self.provider_attempts > 0):
            raise ValueError("logical use must match provider attempts")
        if self.status == "succeeded":
            if self.response_checksum is None or self.error_code is not None:
                raise ValueError("successful calls require a checksum and no error")
        elif self.error_code is None:
            raise ValueError("failed calls require an error code")
        return self


class SerpMarketResultRecord(StrictModel):
    record_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    call_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    query: str = Field(min_length=1, max_length=300)
    result_type: SerpResultType
    position: int = Field(ge=1, le=10_000)
    rank_source: SerpRankSource
    display_name: str = Field(min_length=1, max_length=240)
    url: HttpUrl | None = None
    normalized_domain: str | None = Field(default=None, max_length=253)
    provider_place_id: str | None = Field(default=None, max_length=500)
    provider_data_id: str | None = Field(default=None, max_length=500)
    provider_cid: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=500)
    phone: str | None = Field(default=None, max_length=100)
    categories: list[SerpCategory] = Field(default_factory=list, max_length=20)
    rating: float | None = Field(default=None, ge=0, le=5)
    review_count: int | None = Field(default=None, ge=0, le=1_000_000_000)
    snippet: str | None = Field(default=None, max_length=2_000)
    provider_search_id: str | None = Field(default=None, max_length=200)
    response_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    observed_at: AwareDatetime
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    country_code: str = Field(min_length=2, max_length=2, pattern=r"^[A-Z]{2}$")
    language: str = Field(min_length=2, max_length=12, pattern=r"^[A-Za-z0-9-]+$")
    device: SearchDevice

    @model_validator(mode="after")
    def validate_url_domain(self) -> "SerpMarketResultRecord":
        if self.url is None and self.normalized_domain is not None:
            raise ValueError("domain requires a URL")
        if self.url is not None:
            host = (urlsplit(str(self.url)).hostname or "").casefold().removeprefix("www.")
            if self.normalized_domain != host:
                raise ValueError("normalized domain must match result URL")
        if len(set(self.categories)) != len(self.categories):
            raise ValueError("categories must be unique")
        return self


class SerpQueryRun(StrictModel):
    query_index: int = Field(ge=1, le=SERP_QUERY_MAX)
    query: str = Field(min_length=1, max_length=300)
    maps_call: SerpCallRecord
    google_call: SerpCallRecord
    results: list[SerpMarketResultRecord] = Field(default_factory=list, max_length=60)
    complete: bool
    limitations: list[SerpLimitation] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_run(self) -> "SerpQueryRun":
        if self.maps_call.engine != "google_maps" or self.google_call.engine != "google":
            raise ValueError("query run requires maps and google calls")
        calls = {self.maps_call.call_id: self.maps_call, self.google_call.call_id: self.google_call}
        for call in calls.values():
            if call.query != self.query:
                raise ValueError("call query must match query run")
        expected_complete = all(call.status == "succeeded" for call in calls.values())
        if self.complete != expected_complete:
            raise ValueError("complete flag must match call status")
        counts = Counter(result.result_type for result in self.results)
        if any(count > SERP_RESULT_LIMIT_PER_TYPE for count in counts.values()):
            raise ValueError("result type exceeds per-query limit")
        for result in self.results:
            call = calls.get(result.call_id)
            if call is None or result.query != self.query or result.result_type not in call.result_types:
                raise ValueError("result must belong to a compatible query call")
            if (
                result.latitude,
                result.longitude,
                result.country_code,
                result.language,
                result.device,
            ) != (
                call.latitude,
                call.longitude,
                call.country_code,
                call.language,
                call.device,
            ):
                raise ValueError("result context must match its call")
        return self


class SerpMarketBudget(StrictModel):
    logical_call_limit: Literal[10] = SERP_LOGICAL_CALL_LIMIT
    logical_calls_planned: int = Field(ge=6, le=SERP_LOGICAL_CALL_LIMIT)
    logical_calls_used: int = Field(ge=0, le=SERP_LOGICAL_CALL_LIMIT)
    provider_attempt_limit: int = Field(ge=18, le=30)
    provider_attempts: int = Field(ge=0, le=30)
    checkpoint_hits: int = Field(ge=0, le=SERP_LOGICAL_CALL_LIMIT)
    location_resolution_calls: int = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_budget(self) -> "SerpMarketBudget":
        if self.logical_calls_planned % SERP_CALLS_PER_QUERY:
            raise ValueError("logical call plan must contain complete query pairs")
        if self.logical_calls_used > self.logical_calls_planned:
            raise ValueError("logical calls used cannot exceed plan")
        if self.provider_attempt_limit != (
            self.logical_calls_planned * SERP_PROVIDER_ATTEMPT_LIMIT_PER_CALL
        ):
            raise ValueError("provider attempt limit must match logical plan")
        if self.provider_attempts > self.provider_attempt_limit:
            raise ValueError("provider attempts exceed plan")
        return self


class SerpResultCount(StrictModel):
    result_type: SerpResultType
    count: int = Field(ge=0, le=SERP_QUERY_MAX * SERP_RESULT_LIMIT_PER_TYPE)


class SerpMarketSnapshot(StrictModel):
    schema_version: Literal["serp_market_snapshot_v1"]
    job_id: UUID
    started_at: AwareDatetime
    completed_at: AwareDatetime
    target_point: SerpTargetPoint
    device: SearchDevice
    language: str = Field(min_length=2, max_length=12, pattern=r"^[A-Za-z0-9-]+$")
    country_code: str = Field(min_length=2, max_length=2, pattern=r"^[A-Z]{2}$")
    maps_zoom: Literal[14] = SERP_MAP_ZOOM
    queries: list[str] = Field(min_length=SERP_QUERY_MIN, max_length=SERP_QUERY_MAX)
    query_runs: list[SerpQueryRun] = Field(
        min_length=SERP_QUERY_MIN,
        max_length=SERP_QUERY_MAX,
    )
    result_counts: list[SerpResultCount] = Field(min_length=3, max_length=3)
    complete_query_count: int = Field(ge=SERP_QUERY_MIN, le=SERP_QUERY_MAX)
    budget: SerpMarketBudget
    limitations: list[SerpLimitation] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "SerpMarketSnapshot":
        if self.completed_at < self.started_at:
            raise ValueError("snapshot completion cannot precede start")
        normalized_queries = [query.casefold() for query in self.queries]
        if len(set(normalized_queries)) != len(normalized_queries):
            raise ValueError("snapshot queries must be unique")
        if self.queries != [run.query for run in self.query_runs]:
            raise ValueError("query runs must preserve query order")
        if [run.query_index for run in self.query_runs] != list(range(1, len(self.queries) + 1)):
            raise ValueError("query indexes must be contiguous")
        expected_complete = sum(run.complete for run in self.query_runs)
        if self.complete_query_count != expected_complete:
            raise ValueError("complete query count must match runs")
        if self.budget.logical_calls_planned != len(self.queries) * SERP_CALLS_PER_QUERY:
            raise ValueError("budget plan must match queries")
        if self.country_code != self.target_point.country_code:
            raise ValueError("snapshot country must match target point")
        expected_context = (
            self.target_point.latitude,
            self.target_point.longitude,
            self.country_code,
            self.language,
            self.device,
        )
        all_results: list[SerpMarketResultRecord] = []
        call_ids: list[str] = []
        for run in self.query_runs:
            for call in (run.maps_call, run.google_call):
                call_ids.append(call.call_id)
                if (
                    call.latitude,
                    call.longitude,
                    call.country_code,
                    call.language,
                    call.device,
                ) != expected_context:
                    raise ValueError("all calls must share snapshot context")
            all_results.extend(run.results)
        if len(set(call_ids)) != len(call_ids):
            raise ValueError("call IDs must be unique")
        record_ids = [result.record_id for result in all_results]
        if len(set(record_ids)) != len(record_ids):
            raise ValueError("result record IDs must be unique")
        supplied_counts = {item.result_type: item.count for item in self.result_counts}
        if set(supplied_counts) != {"maps", "local_pack", "organic"}:
            raise ValueError("result counts must include all result types")
        if supplied_counts != dict(Counter(result.result_type for result in all_results)) | {
            key: 0
            for key in {"maps", "local_pack", "organic"}
            if key not in Counter(result.result_type for result in all_results)
        }:
            raise ValueError("result counts must match records")
        if self.budget.logical_calls_used != sum(
            call.logical_call_used
            for run in self.query_runs
            for call in (run.maps_call, run.google_call)
        ):
            raise ValueError("logical call usage must match call records")
        if self.budget.provider_attempts != sum(
            call.provider_attempts
            for run in self.query_runs
            for call in (run.maps_call, run.google_call)
        ):
            raise ValueError("provider attempts must match call records")
        return self
