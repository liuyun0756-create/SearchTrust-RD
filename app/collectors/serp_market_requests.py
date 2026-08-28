"""Pure, deterministic request planning for v2.2 SERP market collection."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.api.v2.models import AnalyzeRequest
from app.collectors.serp_market_models import (
    SERP_CALLS_PER_QUERY,
    SERP_LOGICAL_CALL_LIMIT,
    SERP_MAP_ZOOM,
    SERP_QUERY_MAX,
    SERP_QUERY_MIN,
    SearchDevice,
    SerpEngine,
    SerpResultType,
    SerpTargetPoint,
)
from app.jobs_v22.digest import request_digest
from app.report_v22.models import StrictModel


class SerpSearchPlanError(ValueError):
    pass


class SerpPlannedCall(StrictModel):
    call_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    request_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    query_index: int = Field(ge=1, le=SERP_QUERY_MAX)
    query: str = Field(min_length=1, max_length=300)
    engine: SerpEngine
    result_types: list[SerpResultType] = Field(min_length=1, max_length=2)
    params: dict[str, str] = Field(min_length=5, max_length=10)

    @model_validator(mode="after")
    def validate_params(self) -> "SerpPlannedCall":
        if "api_key" in self.params or "start" in self.params:
            raise ValueError("planned calls cannot include secrets or pagination")
        if self.params.get("engine") != self.engine or self.params.get("q") != self.query:
            raise ValueError("planned params must match call identity")
        expected = ["maps"] if self.engine == "google_maps" else ["local_pack", "organic"]
        if self.result_types != expected:
            raise ValueError("planned result types must match engine")
        return self


class SerpSearchPlan(StrictModel):
    schema_version: Literal["serp_search_plan_v1"]
    target_point: SerpTargetPoint
    device: SearchDevice
    language: str = Field(min_length=2, max_length=12, pattern=r"^[A-Za-z0-9-]+$")
    country_code: str = Field(min_length=2, max_length=2, pattern=r"^[A-Z]{2}$")
    queries: list[str] = Field(min_length=SERP_QUERY_MIN, max_length=SERP_QUERY_MAX)
    calls: list[SerpPlannedCall] = Field(min_length=6, max_length=SERP_LOGICAL_CALL_LIMIT)

    @model_validator(mode="after")
    def validate_plan(self) -> "SerpSearchPlan":
        if self.country_code != self.target_point.country_code:
            raise ValueError("plan country must match target point")
        if len(self.calls) != len(self.queries) * SERP_CALLS_PER_QUERY:
            raise ValueError("plan must contain two calls per query")
        expected_order = [
            (index, query, engine)
            for index, query in enumerate(self.queries, start=1)
            for engine in ("google_maps", "google")
        ]
        actual_order = [(call.query_index, call.query, call.engine) for call in self.calls]
        if actual_order != expected_order:
            raise ValueError("calls must preserve query and engine order")
        if len({call.request_digest for call in self.calls}) != len(self.calls):
            raise ValueError("planned requests must be unique")
        return self


def _coordinate(value: float) -> str:
    if value == 0:
        return "0"
    return f"{value:.7f}".rstrip("0").rstrip(".")


def request_search_context(request: AnalyzeRequest) -> tuple[SearchDevice, str]:
    if request.parent_report is not None:
        context = request.parent_report.case_context
        return context.search_device, context.search_language
    return "mobile", "en"


def _planned_call(
    *,
    query_index: int,
    query: str,
    engine: SerpEngine,
    target_point: SerpTargetPoint,
    device: SearchDevice,
    language: str,
) -> SerpPlannedCall:
    country = target_point.country_code.casefold()
    latitude = _coordinate(target_point.latitude)
    longitude = _coordinate(target_point.longitude)
    if engine == "google_maps":
        params = {
            "engine": "google_maps",
            "q": query,
            "type": "search",
            "ll": f"@{latitude},{longitude},{SERP_MAP_ZOOM}z",
            "gl": country,
            "hl": language,
            "device": device,
        }
        result_types: list[SerpResultType] = ["maps"]
    else:
        params = {
            "engine": "google",
            "q": query,
            "lat": latitude,
            "lon": longitude,
            "gl": country,
            "hl": language,
            "device": device,
            "num": "20",
        }
        result_types = ["local_pack", "organic"]
    digest = request_digest({"schema_version": "serp_request_v1", "params": params})
    return SerpPlannedCall(
        call_id=request_digest({"schema_version": "serp_call_id_v1", "request": digest}),
        request_digest=digest,
        query_index=query_index,
        query=query,
        engine=engine,
        result_types=result_types,
        params=params,
    )


def build_serp_search_plan(
    *,
    queries: list[str],
    target_point: SerpTargetPoint,
    device: SearchDevice = "mobile",
    language: str = "en",
) -> SerpSearchPlan:
    normalized_queries = [query.strip() for query in queries]
    if not SERP_QUERY_MIN <= len(normalized_queries) <= SERP_QUERY_MAX:
        raise SerpSearchPlanError("SERP requires three to five queries")
    if any(not query or len(query) > 300 for query in normalized_queries):
        raise SerpSearchPlanError("SERP queries must be non-empty and bounded")
    if len({query.casefold() for query in normalized_queries}) != len(normalized_queries):
        raise SerpSearchPlanError("SERP queries must be unique")
    calls = [
        _planned_call(
            query_index=index,
            query=query,
            engine=engine,
            target_point=target_point,
            device=device,
            language=language,
        )
        for index, query in enumerate(normalized_queries, start=1)
        for engine in ("google_maps", "google")
    ]
    if len(calls) > SERP_LOGICAL_CALL_LIMIT:
        raise SerpSearchPlanError("SERP logical call budget exceeded")
    return SerpSearchPlan(
        schema_version="serp_search_plan_v1",
        target_point=target_point,
        device=device,
        language=language,
        country_code=target_point.country_code,
        queries=normalized_queries,
        calls=calls,
    )

