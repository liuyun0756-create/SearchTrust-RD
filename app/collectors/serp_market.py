"""Bounded dual-engine SERP market collection for SearchTrust v2.2."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import ValidationError

from app.collectors.serp_market_models import (
    SERP_MAP_ZOOM,
    SERP_PROVIDER_ATTEMPT_LIMIT_PER_CALL,
    SERP_RESULT_LIMIT_PER_TYPE,
    SerpCallRecord,
    SerpMarketBudget,
    SerpMarketResultRecord,
    SerpMarketSnapshot,
    SerpQueryRun,
    SerpResultCount,
)
from app.collectors.serp_market_requests import SerpPlannedCall, SerpSearchPlan
from app.jobs_v22.digest import request_digest


@dataclass(frozen=True)
class SerpProviderResponse:
    payload: dict[str, Any]
    provider_attempts: int = 1
    logical_call_used: bool = True
    checkpoint_hit: bool = False
    started_at: datetime | None = None
    completed_at: datetime | None = None


class SerpSearchProvider(Protocol):
    async def search(self, call: SerpPlannedCall) -> SerpProviderResponse: ...


class SerpMarketProviderError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        provider_attempts: int = 1,
        logical_call_used: bool = True,
        checkpoint_hit: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.provider_attempts = provider_attempts
        self.logical_call_used = logical_call_used
        self.checkpoint_hit = checkpoint_hit


class SerpMarketCollectionError(RuntimeError):
    def __init__(
        self,
        code: str,
        user_message: str,
        *,
        query_runs: tuple[SerpQueryRun, ...],
        budget: SerpMarketBudget,
        retryable: bool,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.user_message = user_message
        self.query_runs = query_runs
        self.budget = budget
        self.retryable = retryable


def _bounded_text(value: Any, limit: int) -> str | None:
    text = str(value or "").strip()
    return text if text and len(text) <= limit else None


def sanitize_serp_payload(payload: dict[str, Any], engine: str) -> dict[str, Any]:
    """Keep only bounded fields needed by deterministic market normalization."""
    sanitized: dict[str, Any] = {}
    metadata = payload.get("search_metadata")
    if isinstance(metadata, dict):
        sanitized["search_metadata"] = {
            key: value
            for key in ("id", "status")
            if (value := _bounded_text(metadata.get(key), 200)) is not None
        }
    if payload.get("error"):
        sanitized["error"] = _bounded_text(payload.get("error"), 500) or "provider_error"

    sections = ["local_results"]
    if engine == "google":
        sections.append("organic_results")
    string_limits = {
        "title": 240,
        "name": 240,
        "website": 2083,
        "link": 2083,
        "place_id": 500,
        "data_id": 500,
        "cid": 200,
        "address": 500,
        "phone": 100,
        "type": 240,
        "snippet": 2_000,
    }
    for section in sections:
        raw = payload.get(section)
        if raw is None:
            continue
        if not isinstance(raw, list):
            sanitized[section] = "invalid"
            continue
        items: list[Any] = []
        for item in raw[:SERP_RESULT_LIMIT_PER_TYPE]:
            if not isinstance(item, dict):
                items.append(None)
                continue
            clean: dict[str, Any] = {}
            for key, limit in string_limits.items():
                if key in item and (value := _bounded_text(item.get(key), limit)) is not None:
                    clean[key] = value
            for key in ("position", "rating", "reviews", "reviews_count"):
                value = item.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and abs(value) <= 10**12:
                    clean[key] = value
                elif isinstance(value, float) and math.isfinite(value) and abs(value) <= 10**12:
                    clean[key] = value
                elif isinstance(value, str) and len(value) <= 30:
                    clean[key] = value
            for key in ("types", "categories"):
                values = item.get(key)
                if isinstance(values, list):
                    clean[key] = [
                        text
                        for raw_value in values[:20]
                        if (text := _bounded_text(raw_value, 240)) is not None
                    ]
            items.append(clean)
        sanitized[section] = items
    return sanitized


def _safe_url(value: Any) -> tuple[str | None, str | None]:
    text = _bounded_text(value, 2083)
    if text is None:
        return None, None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None, None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        return None, None
    host = parsed.hostname.casefold().removeprefix("www.")
    return text, host if len(host) <= 253 else None


def _number(value: Any, *, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if minimum <= number <= maximum else None


def _integer(value: Any, *, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.replace(",", "").isdigit():
        number = int(value.replace(",", ""))
    else:
        return None
    return number if minimum <= number <= maximum else None


def _categories(item: dict[str, Any]) -> list[str]:
    raw = item.get("types") or item.get("categories") or item.get("type")
    values = raw if isinstance(raw, list) else [raw]
    return list(
        dict.fromkeys(
            text
            for value in values[:20]
            if (text := _bounded_text(value, 240)) is not None
        )
    )


def _provider_cid(item: dict[str, Any], data_id: str | None) -> str | None:
    explicit = _bounded_text(item.get("cid"), 200)
    if explicit:
        return explicit
    if not data_id or ":" not in data_id:
        return None
    tail = data_id.rsplit(":", 1)[-1]
    try:
        return str(int(tail, 16)) if tail.lower().startswith("0x") else None
    except ValueError:
        return None


def _provider_search_id(payload: dict[str, Any]) -> str | None:
    metadata = payload.get("search_metadata")
    if not isinstance(metadata, dict):
        return None
    return _bounded_text(metadata.get("id"), 200)


def _payload_error(payload: dict[str, Any]) -> tuple[str | None, bool]:
    raw_error = payload.get("error")
    if raw_error:
        error = _bounded_text(raw_error, 500) or "provider_error"
        normalized = " ".join(error.casefold().replace("'", " ").split())
        empty = any(
            marker in normalized
            for marker in (
                "hasn t returned any results",
                "has not returned any results",
                "no results found",
                "no results",
                "did not return any results",
            )
        )
        return error, empty
    metadata = payload.get("search_metadata")
    if isinstance(metadata, dict):
        status = str(metadata.get("status") or "").strip().casefold()
        if status and status not in {"success", "cached"}:
            return f"status:{status[:100]}", False
    return None, False


def _result_list(payload: dict[str, Any], key: str) -> list[Any]:
    raw = payload.get(key)
    if not isinstance(raw, list):
        return []
    return raw[:SERP_RESULT_LIMIT_PER_TYPE]


def _normalize_results(
    *,
    call: SerpPlannedCall,
    payload: dict[str, Any],
    response_checksum: str,
    observed_at: datetime,
    plan: SerpSearchPlan,
) -> tuple[list[SerpMarketResultRecord], list[str]]:
    sections = (
        [("maps", "local_results")]
        if call.engine == "google_maps"
        else [("local_pack", "local_results"), ("organic", "organic_results")]
    )
    provider_search_id = _provider_search_id(payload)
    records: list[SerpMarketResultRecord] = []
    limitations: list[str] = []
    seen: set[str] = set()
    for result_type, section in sections:
        raw_section = payload.get(section)
        if raw_section is not None and not isinstance(raw_section, list):
            limitations.append(f"SERP_{result_type.upper()}_SECTION_INVALID")
            continue
        for response_index, item in enumerate(_result_list(payload, section), start=1):
            if not isinstance(item, dict):
                limitations.append(f"SERP_{result_type.upper()}_RESULT_SKIPPED")
                continue
            display_name = _bounded_text(item.get("title") or item.get("name"), 240)
            if display_name is None:
                limitations.append(f"SERP_{result_type.upper()}_RESULT_SKIPPED")
                continue
            provider_position = _integer(item.get("position"), minimum=1, maximum=10_000)
            position = provider_position or response_index
            rank_source = "provider_position" if provider_position is not None else "response_order"
            url_value = item.get("website") if result_type != "organic" else item.get("link")
            if not url_value:
                url_value = item.get("link")
            url, domain = _safe_url(url_value)
            if url is not None and domain is None:
                url = None
            place_id = _bounded_text(item.get("place_id"), 500)
            data_id = _bounded_text(item.get("data_id"), 500)
            identity = {
                "call_id": call.call_id,
                "result_type": result_type,
                "position": position,
                "place_id": place_id,
                "data_id": data_id,
                "domain": domain,
                "display_name": display_name.casefold(),
            }
            record_id = request_digest({"schema_version": "serp_result_v1", **identity})
            if record_id in seen:
                limitations.append(f"SERP_{result_type.upper()}_DUPLICATE_SKIPPED")
                continue
            seen.add(record_id)
            try:
                record = SerpMarketResultRecord(
                    record_id=record_id,
                    call_id=call.call_id,
                    query=call.query,
                    result_type=result_type,
                    position=position,
                    rank_source=rank_source,
                    display_name=display_name,
                    url=url,
                    normalized_domain=domain,
                    provider_place_id=place_id,
                    provider_data_id=data_id,
                    provider_cid=_provider_cid(item, data_id),
                    address=_bounded_text(item.get("address"), 500),
                    phone=_bounded_text(item.get("phone"), 100),
                    categories=_categories(item),
                    rating=_number(item.get("rating"), minimum=0, maximum=5),
                    review_count=_integer(
                        item["reviews"] if "reviews" in item else item.get("reviews_count"),
                        minimum=0,
                        maximum=1_000_000_000,
                    ),
                    snippet=_bounded_text(item.get("snippet"), 2_000),
                    provider_search_id=provider_search_id,
                    response_checksum=response_checksum,
                    observed_at=observed_at,
                    latitude=plan.target_point.latitude,
                    longitude=plan.target_point.longitude,
                    country_code=plan.country_code,
                    language=plan.language,
                    device=plan.device,
                )
            except ValidationError:
                limitations.append(f"SERP_{result_type.upper()}_RESULT_SKIPPED")
                continue
            records.append(record)
    return records, list(dict.fromkeys(limitations))[:20]


def _failed_call(
    call: SerpPlannedCall,
    plan: SerpSearchPlan,
    *,
    error: SerpMarketProviderError,
    started_at: datetime,
    completed_at: datetime,
) -> SerpCallRecord:
    return SerpCallRecord(
        call_id=call.call_id,
        request_digest=call.request_digest,
        query=call.query,
        engine=call.engine,
        result_types=call.result_types,
        latitude=plan.target_point.latitude,
        longitude=plan.target_point.longitude,
        country_code=plan.country_code,
        language=plan.language,
        device=plan.device,
        maps_zoom=SERP_MAP_ZOOM if call.engine == "google_maps" else None,
        status="failed_transient" if error.retryable else "failed_deterministic",
        started_at=started_at,
        completed_at=completed_at,
        logical_call_used=error.logical_call_used,
        provider_attempts=error.provider_attempts,
        checkpoint_hit=error.checkpoint_hit,
        error_code=error.code,
    )


class SerpMarketCollector:
    def __init__(
        self,
        *,
        provider: SerpSearchProvider,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.provider = provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def collect(
        self,
        *,
        job_id: UUID,
        plan: SerpSearchPlan,
        location_resolution_calls: int,
    ) -> SerpMarketSnapshot:
        started_at = self.clock()
        calls: dict[str, SerpCallRecord] = {}
        results: dict[str, list[SerpMarketResultRecord]] = {}
        call_limitations: dict[str, list[str]] = {}
        for planned_call in plan.calls:
            call_started_at = self.clock()
            try:
                response = await self.provider.search(planned_call)
                if (
                    not 0 <= response.provider_attempts <= SERP_PROVIDER_ATTEMPT_LIMIT_PER_CALL
                    or response.logical_call_used != (response.provider_attempts > 0)
                ):
                    raise SerpMarketProviderError(
                        "SERP_PROVIDER_ATTEMPT_LIMIT_INVALID",
                        retryable=False,
                        provider_attempts=0,
                        logical_call_used=False,
                    )
                safe_payload = sanitize_serp_payload(response.payload, planned_call.engine)
                payload_error, is_empty = _payload_error(safe_payload)
                if payload_error and not is_empty:
                    raise SerpMarketProviderError(
                        "SERP_PROVIDER_RESPONSE_ERROR",
                        retryable=False,
                        provider_attempts=response.provider_attempts,
                        logical_call_used=response.logical_call_used,
                        checkpoint_hit=response.checkpoint_hit,
                    )
                completed_at = response.completed_at or self.clock()
                call_started_at = response.started_at or call_started_at
                response_checksum = request_digest(safe_payload)
                normalized_payload = {} if is_empty else safe_payload
                normalized, limitations = _normalize_results(
                    call=planned_call,
                    payload=normalized_payload,
                    response_checksum=response_checksum,
                    observed_at=completed_at,
                    plan=plan,
                )
                if is_empty:
                    limitations.append("SERP_PROVIDER_EMPTY_RESULTS")
                calls[planned_call.call_id] = SerpCallRecord(
                    call_id=planned_call.call_id,
                    request_digest=planned_call.request_digest,
                    query=planned_call.query,
                    engine=planned_call.engine,
                    result_types=planned_call.result_types,
                    latitude=plan.target_point.latitude,
                    longitude=plan.target_point.longitude,
                    country_code=plan.country_code,
                    language=plan.language,
                    device=plan.device,
                    maps_zoom=SERP_MAP_ZOOM if planned_call.engine == "google_maps" else None,
                    status="succeeded",
                    started_at=call_started_at,
                    completed_at=completed_at,
                    provider_search_id=_provider_search_id(safe_payload),
                    response_checksum=response_checksum,
                    logical_call_used=response.logical_call_used,
                    provider_attempts=response.provider_attempts,
                    checkpoint_hit=response.checkpoint_hit,
                )
                results[planned_call.call_id] = normalized
                call_limitations[planned_call.call_id] = limitations
            except SerpMarketProviderError as exc:
                completed_at = self.clock()
                if (
                    not 0 <= exc.provider_attempts <= SERP_PROVIDER_ATTEMPT_LIMIT_PER_CALL
                    or exc.logical_call_used != (exc.provider_attempts > 0)
                ):
                    exc = SerpMarketProviderError(
                        "SERP_PROVIDER_ATTEMPT_LIMIT_INVALID",
                        retryable=False,
                        provider_attempts=0,
                        logical_call_used=False,
                    )
                calls[planned_call.call_id] = _failed_call(
                    planned_call,
                    plan,
                    error=exc,
                    started_at=call_started_at,
                    completed_at=completed_at,
                )
                results[planned_call.call_id] = []
                call_limitations[planned_call.call_id] = [exc.code]

        query_runs: list[SerpQueryRun] = []
        for query_index, query in enumerate(plan.queries, start=1):
            planned_maps = plan.calls[(query_index - 1) * 2]
            planned_google = plan.calls[(query_index - 1) * 2 + 1]
            maps_call = calls[planned_maps.call_id]
            google_call = calls[planned_google.call_id]
            run_results = results[planned_maps.call_id] + results[planned_google.call_id]
            limitations = list(
                dict.fromkeys(
                    call_limitations[planned_maps.call_id]
                    + call_limitations[planned_google.call_id]
                )
            )[:20]
            query_runs.append(
                SerpQueryRun(
                    query_index=query_index,
                    query=query,
                    maps_call=maps_call,
                    google_call=google_call,
                    results=run_results,
                    complete=(
                        maps_call.status == "succeeded" and google_call.status == "succeeded"
                    ),
                    limitations=limitations,
                )
            )

        all_calls = [
            call
            for run in query_runs
            for call in (run.maps_call, run.google_call)
        ]
        budget = SerpMarketBudget(
            logical_calls_planned=len(plan.calls),
            logical_calls_used=sum(call.logical_call_used for call in all_calls),
            provider_attempt_limit=len(plan.calls) * SERP_PROVIDER_ATTEMPT_LIMIT_PER_CALL,
            provider_attempts=sum(call.provider_attempts for call in all_calls),
            checkpoint_hits=sum(call.checkpoint_hit for call in all_calls),
            location_resolution_calls=location_resolution_calls,
        )
        complete_query_count = sum(run.complete for run in query_runs)
        if complete_query_count < 3:
            retryable = any(
                call.status == "failed_transient"
                for call in all_calls
            )
            raise SerpMarketCollectionError(
                "SERP_MARKET_INSUFFICIENT_COMPLETE_QUERIES",
                "At least three comparable search query groups are required.",
                query_runs=tuple(query_runs),
                budget=budget,
                retryable=retryable,
            )

        all_results = [result for run in query_runs for result in run.results]
        counts = Counter(result.result_type for result in all_results)
        limitations = list(
            dict.fromkeys(
                limitation
                for run in query_runs
                for limitation in run.limitations
            )
        )[:100]
        return SerpMarketSnapshot(
            schema_version="serp_market_snapshot_v1",
            job_id=job_id,
            started_at=started_at,
            completed_at=self.clock(),
            target_point=plan.target_point,
            device=plan.device,
            language=plan.language,
            country_code=plan.country_code,
            maps_zoom=SERP_MAP_ZOOM,
            queries=plan.queries,
            query_runs=query_runs,
            result_counts=[
                SerpResultCount(result_type=result_type, count=counts[result_type])
                for result_type in ("maps", "local_pack", "organic")
            ],
            complete_query_count=complete_query_count,
            budget=budget,
            limitations=limitations,
        )
