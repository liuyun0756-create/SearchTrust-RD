from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from uuid import UUID

import fakeredis.aioredis
import httpx
import pytest

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.cost_ledger import JobCostLedger
from app.jobs_v22.cost_models import PricingCatalog, empty_request_prices
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from test_v22_competitor_collection_stage import request


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def payload(*, cid="123", website="https://example.test/"):
    return {
        "search_metadata": {"status": "Success", "id": "provider-search"},
        "place_results": {
            "title": "Example Plumbing",
            "website": website,
            "address": "123 Fixture Street, Austin, TX",
            "phone": "+1 512 555 0100",
            "data_id": f"0xabc:0x{int(cid):x}",
            "place_id": "ChIJfixture",
            "cid": cid,
            "service_area": ["Austin", "Round Rock"],
            "service_area_business": True,
        },
    }


def ledger(redis):
    prices = empty_request_prices()
    prices["serpapi"] = 100
    return JobCostLedger(redis, prefix="test:v22", job_id=JOB_ID, ttl_seconds=3600,
        pricing=PricingCatalog(request_usd_micros=prices), job_created_at=NOW,
        clock=lambda: NOW)


@pytest.mark.anyio
async def test_collection_rotates_three_keys_accounts_attempts_and_restarts_from_checkpoint(caplog):
    from app.jobs_v22.customer_public_gbp_stage import (
        CheckpointedCustomerPublicGbpStage, SerpApiCustomerPublicGbpProvider)

    calls = []
    def handler(call: httpx.Request):
        calls.append(call)
        key = call.url.params["api_key"]
        if key == "first-secret":
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json=payload())
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = SerpApiCustomerPublicGbpProvider(keys=["first-secret", "second-secret", "third-secret"],
        base_url="https://serpapi.example/search", connect_timeout=1, read_timeout=1,
        total_timeout=2, max_response_bytes=100_000, client_factory=lambda: client)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints = JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604800)
    stage = CheckpointedCustomerPublicGbpStage(provider, clock=lambda: NOW)
    with caplog.at_level(logging.INFO):
        first = await stage.collect(job_id=JOB_ID, request=request(),
            submitted_at=NOW - timedelta(minutes=1), checkpoints=checkpoints,
            cost_ledger=ledger(redis))
    second = await CheckpointedCustomerPublicGbpStage(provider,
        clock=lambda: (_ for _ in ()).throw(AssertionError("checkpoint restart called clock"))).collect(
            job_id=JOB_ID, request=request(), submitted_at=NOW - timedelta(minutes=1),
            checkpoints=checkpoints, cost_ledger=ledger(redis))

    assert first == second
    assert first.snapshot.health_status == "healthy"
    assert first.snapshot.identity_match_status == "matched"
    assert first.snapshot.expires_at == NOW + timedelta(days=30)
    assert first.reference.entity_keys[0].kind == "cid"
    assert first.reference.entity_keys[0].value == "123"
    assert len(calls) == 2
    assert "first-secret" not in caplog.text and "second-secret" not in caplog.text
    state = await ledger(redis).state()
    assert [claim.operation for claim in state.claims] == [
        "serpapi_customer_public_gbp", "serpapi_customer_public_gbp"]
    assert [claim.outcome for claim in state.claims] == ["failure", "success"]
    await client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["oversized", "timeout", "compressed"])
async def test_transport_is_byte_and_total_timeout_bounded(mode):
    from app.jobs_v22.customer_public_gbp_stage import (
        CheckpointedCustomerPublicGbpStage, SerpApiCustomerPublicGbpProvider)

    async def handler(call: httpx.Request):
        if mode == "timeout":
            await asyncio.sleep(.05)
        return httpx.Response(200, content=b"{" + b"x" * 1000 + b"}",
            headers={"content-encoding": "gzip"} if mode == "compressed" else None)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = SerpApiCustomerPublicGbpProvider(keys=["fixture-key"],
        base_url="https://serpapi.example/search", connect_timeout=1, read_timeout=1,
        total_timeout=.01 if mode == "timeout" else 2,
        max_response_bytes=10_000 if mode == "compressed" else 100,
        client_factory=lambda: client)
    stage = CheckpointedCustomerPublicGbpStage(provider, clock=lambda: NOW)
    redis = fakeredis.aioredis.FakeRedis()
    with pytest.raises(TransientJobError) as raised:
        await stage.collect(job_id=JOB_ID, request=request(), submitted_at=NOW,
            checkpoints=JobCheckpoints(redis, prefix="test:v22", ttl_seconds=3600),
            cost_ledger=ledger(redis))
    assert raised.value.error_code == "V22_CUSTOMER_PUBLIC_GBP_UNAVAILABLE"
    assert [claim.outcome for claim in (await ledger(redis).state()).claims] == ["failure"]
    await client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("response", [
    {"place_results": "bad"}, payload(cid="999"), payload(website="https://wrong.test/")])
async def test_malformed_or_identity_mismatched_result_fails_closed(response):
    from app.jobs_v22.customer_public_gbp_stage import CheckpointedCustomerPublicGbpStage
    class Provider:
        async def request(self, params, *, before_attempt, after_attempt):
            await before_attempt(1, "fingerprint")
            await after_attempt(1, "fingerprint", None)
            return response
    with pytest.raises(DeterministicJobError) as raised:
        await CheckpointedCustomerPublicGbpStage(Provider(), clock=lambda: NOW).collect(
            job_id=JOB_ID, request=request(), submitted_at=NOW,
            checkpoints=JobCheckpoints(fakeredis.aioredis.FakeRedis(), prefix="test:v22", ttl_seconds=3600))
    assert raised.value.error_code == "V22_CUSTOMER_PUBLIC_GBP_IDENTITY_INVALID"


@pytest.mark.anyio
async def test_missing_confirmed_strong_id_fails_before_provider_and_logs_no_secret(caplog):
    from app.jobs_v22.customer_public_gbp_stage import CheckpointedCustomerPublicGbpStage
    analyze = request()
    analyze.business_identity.public_gbp_url = "https://www.google.com/maps/place/Example"
    class Provider:
        async def request(self, *args, **kwargs):
            raise AssertionError("provider must not be called")
    with caplog.at_level(logging.DEBUG), pytest.raises(DeterministicJobError) as raised:
        await CheckpointedCustomerPublicGbpStage(Provider(), clock=lambda: NOW).collect(
            job_id=JOB_ID, request=analyze, submitted_at=NOW,
            checkpoints=JobCheckpoints(fakeredis.aioredis.FakeRedis(), prefix="test:v22", ttl_seconds=3600))
    assert raised.value.error_code == "V22_CUSTOMER_PUBLIC_GBP_IDENTITY_INVALID"
    assert "api_key" not in caplog.text and "fixture-key" not in caplog.text
