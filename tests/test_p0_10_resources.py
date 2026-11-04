"""Resource degradation preserves evidence and historical behavior, without network."""
import gzip
import json
from uuid import uuid4

import fakeredis.aioredis
import httpx
import pytest

from app.collectors.site_inventory_fetcher import SiteFetchError
from app.collectors.site_inventory_html import extract_html_structure
from app.collectors.site_inventory_prefix import complete_tag_prefix
from app.collectors.site_inventory_urls import SiteScope
from app.competitors_v22.public_profile_stage import _BoundedClient
from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.resource_limits import select_evidence
from app.report_v22.site_business_errors import SiteBusinessError
from app.report_v22.site_business_facts import build_site_business_facts


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_evidence_selection_round_robins_sources_then_sorts_ids():
    ids = [f"a{i:02}" for i in range(25)] + ["b00", "c00"]
    selected, omitted = select_evidence(ids, lambda key: key[0])
    assert len(selected) == 20 and omitted == 7
    assert {"b00", "c00"} <= set(selected)
    assert selected == sorted(selected)
    assert select_evidence(reversed(ids), lambda key: key[0]) == (selected, omitted)


def test_total_html_limit_keeps_earlier_page_and_never_treats_rest_as_absent():
    from site_business_helpers import two_pages
    request = two_pages('<p><a href="tel:+15125550100">Call</a></p>')
    request.limits.max_html_bytes_total = len(request.source.payload.selected_pages[0].deep_snapshot.html.encode())
    before = canonical_json_bytes(request)
    with pytest.raises(SiteBusinessError, match="LIMIT_EXCEEDED"):
        build_site_business_facts(request)
    result = build_site_business_facts(request, resource_limits=True)
    assert result.pages[0].status == "parsed"
    assert result.pages[1].status == "not_checked"
    assert result.pages[1].diagnostics[0].code == "resource_limit"
    assert all(f.observation_status == "not_checked" for f in result.pages[1].fields.values())
    assert canonical_json_bytes(request) == before


def test_parser_depth_limit_is_not_a_missing_contact_claim():
    from site_business_helpers import request
    value = request("<div>" * 70 + '<a href="tel:+15125550100">Call</a>' + "</div>" * 70)
    with pytest.raises(SiteBusinessError, match="LIMIT_EXCEEDED"):
        build_site_business_facts(value)
    result = build_site_business_facts(value, resource_limits=True)
    assert result.pages[0].status == "not_checked" and not result.candidates
    assert result.pages[0].diagnostics[0].code == "resource_limit"


@pytest.mark.parametrize("tail", [b'<a hr', b'<a href="/bad', b'<a href="/bad>still', '<a href="/坏'.encode()[:-1]])
def test_prefix_drops_incomplete_tags_attributes_and_multibyte_links(tail):
    prefix = b'<a href="/good">Good</a>'
    text = complete_tag_prefix((prefix + tail).decode("utf-8", errors="replace"))
    assert text == prefix.decode()
    structure = extract_html_structure(text, page_url="https://example.com/", scope=SiteScope.from_root("https://example.com/"))
    assert structure.internal_links == ("https://example.com/good",)


@pytest.mark.anyio
async def test_prefix_fetch_is_opt_in_and_checkpoint_modes_are_isolated():
    from test_v22_site_inventory_fetcher import build_fetcher
    from app.jobs_v22.checkpoints import JobCheckpoints
    from app.jobs_v22.site_inventory_stage import CheckpointedSiteFetcher
    body = b"<html>" + b"x" * 150
    fetcher, calls = build_fetcher(lambda req: httpx.Response(200, headers={"content-type": "text/html"}, content=body))
    redis = fakeredis.aioredis.FakeRedis()
    delegate = CheckpointedSiteFetcher(delegate=fetcher, checkpoints=JobCheckpoints(redis, prefix="test", ttl_seconds=100), job_id=uuid4())
    args = dict(scope=SiteScope.from_root("https://example.com/"), max_bytes=100)
    with pytest.raises(SiteFetchError, match="size"):
        await delegate.fetch("https://example.com/", **args)
    result = await delegate.fetch("https://example.com/", **args, allow_prefix=True)
    assert result.truncated and len(result.body) == 100
    count = len(calls)
    assert await delegate.fetch("https://example.com/", **args, allow_prefix=True) == result
    assert len(calls) == count
    with pytest.raises(SiteFetchError, match="size"):
        await delegate.fetch("https://example.com/", **args)
    await redis.aclose()


@pytest.mark.anyio
async def test_prefix_collector_never_reuses_prefix_as_deep_facts():
    from test_v22_site_inventory_collector import collector, FakeFetcher, html_response
    from dataclasses import replace
    url = "https://example.com/"
    prefix = replace(html_response(url, '<title>Incomplete</title><h1>Partial</h1>'), truncated=True)
    fetcher = FakeFetcher({url: [prefix, SiteFetchError("response_too_large", "deep limit")]})
    instance = collector(fetcher)
    instance.resource_limits = True
    result = await instance.collect(site_url=url, discovery_limit=1, deep_analysis_limit=1)
    assert result.schema_version == "site_inventory_snapshot_v2"
    assert result.pages[0].structural_truncated
    assert result.pages[0].title is None and result.pages[0].h1 is None
    assert result.selected_pages[0].deep_snapshot is None
    assert result.deep_attempts[0].error_code == "response_too_large"
    assert fetcher.calls.count(url) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("compressed", [True, False])
async def test_profile_response_decodes_once_and_bounds_decoded_bytes(compressed):
    body = json.dumps({"reviews": [{"snippet": "x" * 1000}]}).encode()
    def handler(req):
        return httpx.Response(200, headers={"content-encoding": "gzip"} if compressed else {},
                              content=gzip.compress(body) if compressed else body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await _BoundedClient(client, 2000).get("https://offline.invalid")).json()["reviews"]
        with pytest.raises(ValueError, match="safe limit"):
            await _BoundedClient(client, 100).get("https://offline.invalid")


@pytest.mark.anyio
@pytest.mark.parametrize("host,expected", [("www.rival-1.example", "available"), ("elsewhere.example", "unavailable")])
async def test_competitor_www_is_allowed_but_other_domain_rejected(host, expected):
    from test_v22_competitor_site_stage import RecordingSiteCollector, confirmed, discovery_request, JOB_ID
    from app.competitors_v22.site_stage import CheckpointedCompetitorSiteStage
    from app.jobs_v22.checkpoints import JobCheckpoints
    competitor = confirmed(1)
    domain = str(competitor.website_url).split("//")[1].strip("/")
    class Collector(RecordingSiteCollector):
        async def collect_inventory_for_site(self, **kwargs):
            result = await super().collect_inventory_for_site(**kwargs)
            return result.model_copy(update={"canonical_host": "www." + domain if expected == "available" else host})
    redis = fakeredis.aioredis.FakeRedis()
    result = await CheckpointedCompetitorSiteStage(Collector()).collect(job_id=JOB_ID, competitors=[competitor],
        primary_service="Plumbing", target_market=discovery_request().target_market, max_pages_each=10,
        checkpoints=JobCheckpoints(redis, prefix="test", ttl_seconds=100))
    assert result[0].status == expected
    await redis.aclose()


def test_eval_isolates_generation_error_without_scoring_it_as_correct(tmp_path, monkeypatch):
    from test_p0_08_gold import synthetic_case
    from scripts import eval_gold
    from app.report_v22.findings_errors import FindingsError
    import shutil
    one, _ = synthetic_case(tmp_path)
    two = tmp_path / "second"
    shutil.copytree(one, two)
    generate = eval_gold.generate
    def failing(descriptor, directory, private_root):
        if directory.name == "synthetic":
            raise FindingsError("LIMIT_EXCEEDED")
        return generate(descriptor, directory, private_root)
    monkeypatch.setattr(eval_gold, "generate", failing)
    result = eval_gold.evaluate_all(tmp_path, tmp_path)
    assert not result["errors"] and len(result["cases"]) == 2
    failed = next(c for c in result["cases"] if c["reason"] == "generation_error")
    assert failed["error_code"] == "V22_FINDINGS_LIMIT_EXCEEDED"
    assert failed["finding_metrics"]["false_positive_rate"] is None
    assert any(c["status"] == "已生成" for c in result["cases"])
    assert result["summary"]["incomplete"] is True


def test_actual_blocked_sites_generate_with_bounded_evidence_and_unchecked_content(monkeypatch):
    from test_p0_08_gold import GOLD, ROOT, deny_network
    from scripts.eval_gold import generate, read_json
    deny_network(monkeypatch)
    private = ROOT.parent / ".release-private/2026-10-29"
    observations = {}
    for name in ("all-star-plumbing-fresno", "seamless-gutters-pensacola", "volcanoes-hiking-guides"):
        directory = GOLD / name
        report, result, request, _, _, _ = generate(read_json(directory / "input.json"), directory, private)
        assert len(report.actions) <= 3
        assert all(len(f.evidence_ids) <= 20 for f in report.findings)
        assert all(len(m.evidence_ids) <= 20 for a in report.actions for m in a.materials)
        observations[name] = report, result, request
    all_star = observations["all-star-plumbing-fresno"][0]
    assert any("Another 6 similar evidence items" in note for f in all_star.findings for note in f.limitations)
    seamless, result, _ = observations["seamless-gutters-pensacola"]
    assert result._resource_limited_pages
    assert any("not checked" in note and "processing limits" in note for note in seamless.limitations)
    assert len(seamless.prioritization.candidates) >= 4
    volcanoes, result, request = observations["volcanoes-hiking-guides"]
    site = next(s for s in request.evidence_input.sources if s.kind == "site")
    assert site.payload.pages[0].structural_truncated
    assert site.payload.pages[0].internal_links == []
    assert site.payload.deep_analyzed_count == 0
    assert result._resource_limited_pages == [str(site.payload.root_url)]
    assert all(c.status in {"not_checked", "not_assessed"} and c.result == "none" for c in volcanoes.contact_checks)


@pytest.mark.parametrize("site", ["pr-plumbing-fresno", "vision-tech-swansea"])
def test_saved_v8_reports_still_replay_byte_identically(site, monkeypatch):
    from test_p0_08_gold import ROOT, deny_network
    from app.report_v22.capability_public import build_public_capability_report
    directory = ROOT.parent / ".release-private/2026-11-03-gold/p0-09-frozen-v2-2026-11-03" / site
    if not (directory / "bound.json").is_file():
        pytest.skip("Private frozen P0-09 v8 inputs unavailable")
    deny_network(monkeypatch)
    bound = json.loads((directory / "bound.json").read_text())
    saved = json.loads((directory / "report.json").read_text())
    assert canonical_json_bytes(build_public_capability_report(bound["manifest"], bound["request"])) == canonical_json_bytes(saved)
