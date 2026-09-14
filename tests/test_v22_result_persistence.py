from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from pydantic import BaseModel

from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.result_persistence import SupabaseResultPersister, result_snapshot_id
from app.report_v22.public_gbp_models import CustomerPublicGbpReference
from public_gbp_helpers import public_source, sample_input


CASE_ID = UUID("11111111-1111-4111-8111-111111111111")
JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)


class Payload(BaseModel):
    schema_version: str
    completed_at: datetime
    limitations: list[str] = []
    data_coverage: object | None = None


class CoverageSource(BaseModel):
    source_type: str
    health_status: str


class Coverage(BaseModel):
    sources: list[CoverageSource]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def inputs():
    site = Payload(schema_version="site_inventory_snapshot_v1", completed_at=NOW)
    serp = Payload(schema_version="serp_market_snapshot_v1", completed_at=NOW)
    competitor = Payload(schema_version="competitor_collection_snapshot_v1", completed_at=NOW)
    request = SimpleNamespace(case_id=CASE_ID)
    shared = SimpleNamespace(
        snapshot_id=UUID("44444444-4444-4444-8444-444444444444"),
        snapshot=serp,
        snapshot_checksum="sha256:" + "b" * 64,
        expires_at=NOW + timedelta(hours=1),
    )
    report = Payload(schema_version="report_v2_2", completed_at=NOW,
        data_coverage=Coverage(sources=[CoverageSource(
            source_type="gbp", health_status="healthy")]))
    return request, site, shared, competitor, report


def public_values():
    raw = sample_input()
    return public_source(raw).payload, CustomerPublicGbpReference.model_validate(raw["reference"])


@pytest.mark.anyio
async def test_persister_sends_exact_sources_to_one_service_role_rpc() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json=[{"report_id": str(JOB_ID), "idempotent": False}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        persister = SupabaseResultPersister(
            url="https://project.supabase.co/",
            service_role_key="service-secret",
            http_client=client,
        )
        request, site, shared, competitor, report = inputs()
        public, reference = public_values()
        await persister.persist(
            job_id=JOB_ID,
            request=request,
            site_inventory=site,
            shared_market=shared,
            competitor_collection=competitor,
            report=report,
            public_gbp_snapshot=public,
            public_gbp_reference=reference,
        )

    sent = captured["request"]
    assert str(sent.url).endswith("/rest/v1/rpc/persist_v22_prospect_result")
    assert sent.headers["authorization"] == "Bearer service-secret"
    body = __import__("json").loads(sent.content)
    assert body["p_site_payload"]["schema_version"] == "site_inventory_snapshot_v1"
    assert body["p_serp_payload"]["schema_version"] == "serp_market_snapshot_v1"
    assert body["p_competitor_payload"]["schema_version"] == "competitor_collection_snapshot_v1"
    assert body["p_site_snapshot_id"] == str(
        result_snapshot_id("site", CASE_ID, body["p_site_checksum"])
    )


@pytest.mark.anyio
async def test_persister_sends_public_gbp_snapshot_and_exact_reference_atomically() -> None:
    captured = {}
    raw = sample_input()
    public = public_source(raw)
    reference = CustomerPublicGbpReference.model_validate(raw["reference"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: (captured.setdefault("request", request),
                httpx.Response(200, json=[{"report_id": str(JOB_ID), "idempotent": False}]))[1])) as client:
        request, site, shared, competitor, report = inputs()
        await SupabaseResultPersister(url="https://project.supabase.co", service_role_key="secret",
            http_client=client).persist(job_id=JOB_ID, request=request, site_inventory=site,
                shared_market=shared, competitor_collection=competitor, report=report,
                public_gbp_snapshot=public.payload, public_gbp_reference=reference)
    body = __import__("json").loads(captured["request"].content)
    assert body["p_public_gbp_payload"] == public.payload.model_dump(mode="json")
    assert body["p_public_gbp_reference"] == reference.model_dump(mode="json")
    assert body["p_public_gbp_snapshot_id"] == str(result_snapshot_id(
        "public_gbp", CASE_ID, body["p_public_gbp_checksum"]))


@pytest.mark.anyio
async def test_persister_requires_public_gbp_source_arguments() -> None:
    request, site, shared, competitor, _ = inputs()
    report = SimpleNamespace(data_coverage=SimpleNamespace(sources=[
        SimpleNamespace(source_type="gbp", health_status="healthy")]))
    async with httpx.AsyncClient() as client:
        with pytest.raises(TypeError):
            await SupabaseResultPersister(url="https://project.supabase.co", service_role_key="secret",
                http_client=client).persist(job_id=JOB_ID, request=request, site_inventory=site,
                    shared_market=shared, competitor_collection=competitor, report=report)


@pytest.mark.anyio
async def test_persister_classifies_storage_outages_as_retryable() -> None:
    async def run(status: int) -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(status, json={}))
        ) as client:
            persister = SupabaseResultPersister(
                url="https://project.supabase.co",
                service_role_key="secret",
                http_client=client,
            )
            request, site, shared, competitor, report = inputs()
            public, reference = public_values()
            await persister.persist(
                job_id=JOB_ID,
                request=request,
                site_inventory=site,
                shared_market=shared,
                competitor_collection=competitor,
                report=report,
                public_gbp_snapshot=public,
                public_gbp_reference=reference,
            )

    with pytest.raises(TransientJobError):
        await run(503)
    with pytest.raises(DeterministicJobError):
        await run(400)
