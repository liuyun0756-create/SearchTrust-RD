from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import fakeredis.aioredis
import httpx
import pytest

from app.api.v2.models import AnalyzeRequest
from app.collectors.serp_market import (
    SerpMarketProviderError,
    SerpProviderResponse,
)
from app.collectors.serp_market_location import LocationLookupResponse
from app.collectors.serp_market_requests import SerpPlannedCall, build_serp_search_plan
from app.collectors.serp_market_requests import serp_market_context_from_analyze
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.serp_market_stage import (
    _BoundedHttpClient,
    CheckpointedSerpMarketStage,
    CheckpointedSerpProvider,
    SerpMarketCheckpointError,
    serp_market_cost_counters,
)


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = ROOT / "contracts" / "v2.2"
FIXTURES = Path(__file__).parent / "fixtures" / "v22_serp_market"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def prospect_request(*, coordinates: bool = True) -> AnalyzeRequest:
    report = json.loads((CONTRACT_DIR / "fixtures" / "prospect.json").read_text())
    competitors = report["competitor_analysis"]["competitors"]
    target_market = dict(report["case_context"]["target_market"])
    if not coordinates:
        target_market["latitude"] = None
        target_market["longitude"] = None
        target_market["postal_code"] = None
    payload = {
        "case_id": report["identity"]["case_id"],
        "report_type": "prospect",
        "business_identity": report["identity"]["business"],
        "primary_service": report["case_context"]["primary_service"],
        "target_market": target_market,
        "queries": report["case_context"]["queries"],
        "competitors": [
            {
                "competitor_id": competitor["competitor_id"],
                "business_name": competitor["business_name"],
                "website_url": competitor["website_url"],
                "public_gbp_url": competitor["public_gbp_url"],
                "confirmation_source": "user",
            }
            for competitor in competitors
        ],
        "first_party_snapshots": [],
        "parent_report": None,
        "generation_limits": report.get("generation_limits", {}),
    }
    return AnalyzeRequest.model_validate_json(json.dumps(payload))


def payload_for(call: SerpPlannedCall) -> dict:
    name = "google-maps.json" if call.engine == "google_maps" else "google-search.json"
    return json.loads((FIXTURES / name).read_text())


class UnusedLocationProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def search(self, query: str) -> LocationLookupResponse:
        self.calls += 1
        raise AssertionError(query)


class AustinLocationProvider:
    def __init__(self) -> None:
        self.calls = 0
        raw = (FIXTURES / "locations-austin.json").read_bytes()
        self.response = LocationLookupResponse(
            items=tuple(json.loads(raw)),
            response_checksum=f"sha256:{hashlib.sha256(raw).hexdigest()}",
        )

    async def search(self, query: str) -> LocationLookupResponse:
        self.calls += 1
        return self.response


class InterruptingSearchProvider:
    def __init__(self) -> None:
        self.network_calls: list[tuple[str, str]] = []
        self.interrupted = False

    async def search(self, call: SerpPlannedCall, *, before_attempt) -> SerpProviderResponse:
        await before_attempt(1, "a" * 12)
        self.network_calls.append((call.query, call.engine))
        if len(self.network_calls) == 4 and not self.interrupted:
            self.interrupted = True
            raise asyncio.CancelledError()
        return SerpProviderResponse(
            payload_for(call),
            started_at=NOW,
            completed_at=NOW,
        )


class SuccessfulSearchProvider:
    def __init__(self) -> None:
        self.network_calls = 0

    async def search(self, call: SerpPlannedCall, *, before_attempt) -> SerpProviderResponse:
        await before_attempt(1, "b" * 12)
        self.network_calls += 1
        return SerpProviderResponse(
            payload_for(call),
            started_at=NOW,
            completed_at=NOW,
        )


def checkpoints():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    return JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604800)


@pytest.mark.anyio
async def test_bounded_http_client_requests_identity_encoding() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, json={"search_metadata": {"status": "Success"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await _BoundedHttpClient(
            client,
            max_response_bytes=10_000,
        ).get("https://serpapi.example/search.json")

    assert response.json()["search_metadata"]["status"] == "Success"


@pytest.mark.anyio
async def test_stage_resumes_only_missing_searches_and_reuses_final_snapshot() -> None:
    saved = checkpoints()
    location = UnusedLocationProvider()
    search = InterruptingSearchProvider()
    stage = CheckpointedSerpMarketStage(
        location_provider=location,
        search_provider=search,
        clock=lambda: NOW,
    )

    with pytest.raises(asyncio.CancelledError):
        await stage.collect(
            job_id=JOB_ID,
            request=prospect_request(),
            checkpoints=saved,
        )

    snapshot = await stage.collect(
        job_id=JOB_ID,
        request=prospect_request(),
        checkpoints=saved,
    )
    calls_after_success = list(search.network_calls)
    repeated = await stage.collect(
        job_id=JOB_ID,
        request=prospect_request(),
        checkpoints=saved,
    )

    assert repeated == snapshot
    assert location.calls == 0
    assert len(search.network_calls) == 7
    assert search.network_calls == calls_after_success
    assert snapshot.budget.logical_calls_used == 6
    assert snapshot.budget.provider_attempts == 7
    assert snapshot.budget.checkpoint_hits == 3
    assert serp_market_cost_counters(snapshot) == {
        "serp_logical_calls": 6,
        "serp_provider_attempts": 7,
        "serp_checkpoint_hits": 3,
        "serp_location_calls": 0,
    }


@pytest.mark.anyio
async def test_location_lookup_is_checkpointed_once() -> None:
    saved = checkpoints()
    location = AustinLocationProvider()
    search = SuccessfulSearchProvider()
    stage = CheckpointedSerpMarketStage(
        location_provider=location,
        search_provider=search,
        clock=lambda: NOW,
    )

    first = await stage.collect(
        job_id=JOB_ID,
        request=prospect_request(coordinates=False),
        checkpoints=saved,
    )
    repeated = await stage.collect(
        job_id=JOB_ID,
        request=prospect_request(coordinates=False),
        checkpoints=saved,
    )

    assert first == repeated
    assert first.target_point.provider_location_id == "city-austin"
    assert first.budget.location_resolution_calls == 1
    assert location.calls == 1
    assert search.network_calls == 6


@pytest.mark.anyio
async def test_context_entrypoint_matches_analyze_wrapper() -> None:
    request = prospect_request()
    context = serp_market_context_from_analyze(request)
    first_checkpoints = checkpoints()
    second_checkpoints = checkpoints()
    first_search = SuccessfulSearchProvider()
    second_search = SuccessfulSearchProvider()
    wrapper = CheckpointedSerpMarketStage(
        location_provider=UnusedLocationProvider(),
        search_provider=first_search,
        clock=lambda: NOW,
    )
    direct = CheckpointedSerpMarketStage(
        location_provider=UnusedLocationProvider(),
        search_provider=second_search,
        clock=lambda: NOW,
    )

    wrapped = await wrapper.collect(job_id=JOB_ID, request=request, checkpoints=first_checkpoints)
    contextual = await direct.collect_context(
        job_id=JOB_ID,
        context=context,
        checkpoints=second_checkpoints,
    )

    assert contextual == wrapped
    assert first_search.network_calls == second_search.network_calls == 6


class AlwaysFailingProvider:
    def __init__(self) -> None:
        self.network_calls = 0

    async def search(self, call: SerpPlannedCall, *, before_attempt) -> SerpProviderResponse:
        await before_attempt(1, "c" * 12)
        self.network_calls += 1
        raise SerpMarketProviderError(
            "SERP_PROVIDER_TEMPORARY",
            retryable=True,
        )


@pytest.mark.anyio
async def test_provider_attempt_ledger_stops_fourth_network_call() -> None:
    saved = checkpoints()
    request = prospect_request()
    point = request.target_market
    from app.collectors.serp_market_location import resolve_target_point

    target = await resolve_target_point(point, provider=None, clock=lambda: NOW)
    call = build_serp_search_plan(queries=request.queries, target_point=target).calls[0]
    delegate = AlwaysFailingProvider()
    provider = CheckpointedSerpProvider(
        delegate=delegate,
        checkpoints=saved,
        job_id=JOB_ID,
        clock=lambda: NOW,
    )

    for expected_attempts in (1, 2, 3):
        with pytest.raises(SerpMarketProviderError) as raised:
            await provider.search(call)
        assert raised.value.provider_attempts == expected_attempts
        assert raised.value.retryable is True

    with pytest.raises(SerpMarketProviderError) as exhausted:
        await provider.search(call)

    assert exhausted.value.code == "SERP_PROVIDER_ATTEMPTS_EXHAUSTED"
    assert exhausted.value.retryable is False
    assert exhausted.value.provider_attempts == 3
    assert delegate.network_calls == 3


@pytest.mark.anyio
async def test_bad_search_checkpoint_fails_without_provider_access() -> None:
    saved = checkpoints()
    request = prospect_request()
    from app.collectors.serp_market_location import resolve_target_point

    target = await resolve_target_point(request.target_market, provider=None, clock=lambda: NOW)
    call = build_serp_search_plan(queries=request.queries, target_point=target).calls[0]
    delegate = SuccessfulSearchProvider()
    provider = CheckpointedSerpProvider(
        delegate=delegate,
        checkpoints=saved,
        job_id=JOB_ID,
        clock=lambda: NOW,
    )
    await saved.save(JOB_ID, provider.checkpoint_key(call), {"bad": "payload"})

    with pytest.raises(SerpMarketCheckpointError):
        await provider.search(call)

    assert delegate.network_calls == 0
