import asyncio
import json
from pathlib import Path
from uuid import UUID

import fakeredis.aioredis
import pytest

from app.api.v2.models import AnalyzeRequest
from app.collectors.site_inventory_fetcher import SiteFetchResponse
from app.collectors.site_inventory_firecrawl import FirecrawlMapResult
from app.collectors.site_inventory_urls import SiteScope
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.site_inventory_stage import (
    CheckpointedSiteFetcher,
    CheckpointedSiteInventoryStage,
    SiteInventoryCheckpointError,
    SiteInventoryStageConfig,
)


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
CONTRACT_DIR = Path(__file__).resolve().parents[1] / "contracts" / "v2.2"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def prospect_request() -> AnalyzeRequest:
    report = json.loads((CONTRACT_DIR / "fixtures" / "prospect.json").read_text(encoding="utf-8"))
    competitors = report["competitor_analysis"]["competitors"]
    payload = {
        "case_id": report["identity"]["case_id"],
        "report_type": "prospect",
        "business_identity": {**report["identity"]["business"], "site_url": "https://example.com/"},
        "primary_service": "plumbing",
        "target_market": report["case_context"]["target_market"],
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
        "generation_limits": {
            "max_site_urls": 2,
            "max_deep_pages": 2,
            "max_competitor_pages_each": 10,
            "competitor_count": 3,
            "max_pagespeed_pages": 5,
            "max_review_samples_each": 30,
        },
    }
    return AnalyzeRequest.model_validate_json(json.dumps(payload))


def html_response(url: str, html: str) -> SiteFetchResponse:
    return SiteFetchResponse(
        requested_url=url,
        final_url=url,
        status_code=200,
        content_type="text/html",
        body=html.encode(),
    )


class RecordingFetcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch(self, url: str, **_: object) -> SiteFetchResponse:
        self.calls.append(url)
        if url.endswith("/robots.txt") or url.endswith("/sitemap.xml"):
            return SiteFetchResponse(url, url, 404, "text/plain", b"missing")
        if url == "https://example.com/":
            return html_response(url, "<h1>Home</h1>")
        if url == "https://example.com/a":
            return html_response(url, "<h1>Service A</h1>")
        raise AssertionError(url)

    def apply_crawl_delay(self, seconds: float | None) -> None:
        return None


class InterruptingFirecrawl:
    def __init__(self) -> None:
        self.calls = 0

    async def map(self, root_url: str, *, limit: int) -> FirecrawlMapResult:
        self.calls += 1
        if self.calls == 1:
            raise asyncio.CancelledError()
        return FirecrawlMapResult(("https://example.com/a",), None)


def stage(fetcher: RecordingFetcher, firecrawl: InterruptingFirecrawl) -> CheckpointedSiteInventoryStage:
    return CheckpointedSiteInventoryStage(
        fetcher=fetcher,
        firecrawl=firecrawl,
        config=SiteInventoryStageConfig(
            structural_max_bytes=256_000,
            deep_max_bytes=2_000_000,
            sitemap_max_bytes=100_000,
            sitemap_decompressed_max_bytes=500_000,
            sitemap_max_files=5,
            sitemap_max_depth=2,
            batch_size=2,
        ),
    )


@pytest.mark.anyio
async def test_stage_resumes_external_calls_and_reuses_final_snapshot() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints = JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604800)
    fetcher = RecordingFetcher()
    firecrawl = InterruptingFirecrawl()
    inventory_stage = stage(fetcher, firecrawl)

    with pytest.raises(asyncio.CancelledError):
        await inventory_stage.collect(
            job_id=JOB_ID,
            request=prospect_request(),
            checkpoints=checkpoints,
        )

    result = await inventory_stage.collect(
        job_id=JOB_ID,
        request=prospect_request(),
        checkpoints=checkpoints,
    )
    calls_after_success = list(fetcher.calls)
    repeated = await inventory_stage.collect(
        job_id=JOB_ID,
        request=prospect_request(),
        checkpoints=checkpoints,
    )

    assert result == repeated
    assert result.discovered_url_count == 2
    assert fetcher.calls == calls_after_success
    assert fetcher.calls.count("https://example.com/") == 1
    assert fetcher.calls.count("https://example.com/robots.txt") == 1
    assert fetcher.calls.count("https://example.com/sitemap.xml") == 1
    assert fetcher.calls.count("https://example.com/a") == 1
    assert firecrawl.calls == 2


@pytest.mark.anyio
async def test_bad_fetch_checkpoint_fails_without_network_access() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints = JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604800)
    delegate = RecordingFetcher()
    wrapper = CheckpointedSiteFetcher(
        delegate=delegate,
        checkpoints=checkpoints,
        job_id=JOB_ID,
    )
    scope = SiteScope.from_root("https://example.com/")
    key = wrapper.checkpoint_key(
        "https://example.com/",
        max_bytes=256_000,
        accepted_media_types={"text/html"},
    )
    await checkpoints.save(JOB_ID, key, {"bad": "payload"})

    with pytest.raises(SiteInventoryCheckpointError):
        await wrapper.fetch(
            "https://example.com/",
            scope=scope,
            max_bytes=256_000,
            accepted_media_types={"text/html"},
        )

    assert delegate.calls == []
