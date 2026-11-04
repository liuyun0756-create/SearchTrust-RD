"""V10 opt-in safeguards and fixed v9 gold baseline. All checks are offline."""
import json
from pathlib import Path
from types import SimpleNamespace as Obj

import pytest

from app.collectors.site_page_scope import is_admin_page
from app.report_v22.sample_locality import audit_sample, classify_record, LOCALITY_NOTE
from app.report_v22.site_gbp_comparators import compare_business_names

MARKET = Obj(city="Pensacola", region="FL", country_code="US")


def record(title="Plumbing in Helena, AL", snippet="", url="https://example.test/a"):
    return dict(display_name=title, snippet=snippet, address=None, url=url, position=1, query="Plumbing near me")


@pytest.mark.parametrize("snippet,expected", [
    ("12.9 mi · East Birmingham, AL 35043", "non_local"),
    ("Our office is based in Wichita but serves the surrounding area", "unknown"),
    ("Serving Fort Walton Beach, FL", "unknown"),
    ("Serving Pensacola, FL", "unknown"),
    ("Northwest Florida LLC · Bel Air · Alaska Solutions", "non_local"),
])
def test_multiple_location_ruling_never_guesses_a_missing_state(snippet, expected):
    row = classify_record(record(snippet=snippet), MARKET)
    assert row["state"] == expected
    assert row["signals"]


def test_unknowns_and_duplicate_urls_cannot_manufacture_a_majority():
    other = record(url="https://other.test/")
    local = record("Plumbing in Pensacola, FL", url="https://local.test/")
    unknown = record("Plumbing services", url="https://unknown.test/")
    assert not audit_sample([other, local], MARKET)["non_local_majority"]
    result = audit_sample([other, record(url="https://second.test/"), local, unknown], MARKET)
    assert (result["N"], result["O"], result["L"]) == (4, 2, 1)
    assert not result["non_local_majority"]
    assert not audit_sample([other, other, other, local], MARKET)["non_local_majority"]
    result = audit_sample([other, record("Plumbing in Pensacola, FL", url=other["url"]), unknown], MARKET)
    assert result["O"] == 0 and result["unknown"] == 2
    result = audit_sample([record(url=None), other, unknown], MARKET)
    assert result["N"] == 3 and result["O"] == 1
    assert classify_record(record("Alaska Solutions · Bel Air", snippet="Northwest Florida LLC"), MARKET)["state"] == "unknown"


def test_sampling_coordinates_never_determine_result_locality():
    raw = record()
    raw.update(latitude=30.42, longitude=-87.21, country_code="US")
    assert classify_record(raw, MARKET)["state"] == "non_local"
    raw.update(display_name="Plumbing services")
    assert classify_record(raw, MARKET)["state"] == "unknown"


@pytest.mark.parametrize("url,excluded", [
    ("/wp-login.php", True), ("/WP-ADMIN/edit.php", True), ("/%77p-admin/", True),
    ("/service?other=1&action=", True), ("/service?%61ction=login", True),
    ("/transaction", False), ("/action-plans/", False), ("/service?transaction=1", False),
    ("/category/news", False), ("/tag/roofing", False), ("/wp-json/", False), ("/xmlrpc.php", False),
])
def test_admin_scope_is_limited_to_approved_paths_and_parameter_names(url, excluded):
    assert is_admin_page("https://example.test" + url) is excluded


@pytest.mark.parametrize("left,right,city,country,state", [
    ("All Star Plumbing", "All Star Plumbing Fresno", "Fresno", "US", "semantic_match"),
    ("Vision-Tech AV", "Vision Tech AV LTD", "Swansea", "GB", "semantic_match"),
    ("Fresno All Star Plumbing LLC", "All Star Plumbing", "Fresno", "US", "semantic_match"),
    ("All Star Plumbing Fresno Inc.", "All Star Plumbing", "Fresno", "US", "semantic_match"),
    ("Seamless Gutters of Pensacola", "Gutter Solutions & Home Improvements", "Pensacola", "US", "mismatch"),
    ("Fresno Plumbing", "Plumbing", "Fresno", "US", "mismatch"),
    ("All Fresno Star", "All Star", "Fresno", "US", "mismatch"),
])
def test_name_equivalence_is_boundary_only(left, right, city, country, state):
    assert compare_business_names(left, right, country, name_quality=True, city=city).state == state
    if state == "semantic_match":
        assert compare_business_names(left, right, country).state == "mismatch"


@pytest.mark.anyio
async def test_new_collection_excludes_admin_candidates_and_redirects_only_when_enabled():
    from dataclasses import replace
    from test_v22_site_inventory_collector import collector, FakeFetcher, html_response
    root = "https://example.com/"
    pages = {root: html_response(root, '<a href="/wp-login.php">login</a><a href="/category/news">news</a><a href="/redirect">r</a>'),
             root+"wp-login.php": html_response(root+"wp-login.php", '<title>Login</title>'),
             root+"category/news": html_response(root+"category/news", '<title>News</title>'),
             root+"redirect": replace(html_response(root+"redirect", '<title>Admin</title>'), final_url=root+"wp-admin/")}
    old_fetcher, new_fetcher = FakeFetcher(pages), FakeFetcher(pages)
    old, new = collector(old_fetcher), collector(new_fetcher)
    new.sampling_quality = True
    old_result = await old.collect(site_url=root, discovery_limit=10, deep_analysis_limit=10)
    result = await new.collect(site_url=root, discovery_limit=10, deep_analysis_limit=10)
    assert root+"wp-login.php" in old_fetcher.calls
    assert root+"wp-login.php" not in new_fetcher.calls
    assert any(str(p.url) == root+"category/news" for p in result.selected_pages)
    assert all(not is_admin_page(p.url) and not is_admin_page(p.deep_snapshot.final_url if p.deep_snapshot else None) for p in result.selected_pages)
    assert any("administrative" in note for note in result.limitations)
    assert len(old_result.pages) > len(result.pages)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_six_site_v10_changes_exactly_ten_false_positives_and_preserves_v9_bytes(monkeypatch):
    from scripts.eval_gold import evaluate_all, generate, read_json
    from test_p0_08_gold import GOLD, ROOT, deny_network
    from app.jobs_v22.digest import canonical_json_bytes
    private = ROOT.parent / ".release-private/2026-10-29"
    if not (private / "nova-v5-report.json").is_file():
        pytest.skip("Private Nova input unavailable")
    deny_network(monkeypatch)
    old = evaluate_all(GOLD, private, ruleset="v22_capability_public_v9")
    new = evaluate_all(GOLD, private, ruleset="v22_capability_public_v10")
    assert old["errors"] == new["errors"] == []
    assert old["summary"]["finding_metrics"]["false_positive_rate"] == 10/43
    assert new["summary"]["finding_metrics"]["false_positive_rate"] == 0/33
    assert new["summary"]["finding_metrics"]["judged_generated_findings"] == 33
    assert new["summary"]["field_metrics"]["not_checked_rate"] == 9/26
    changed = 0
    for before, after in zip(old["cases"], new["cases"]):
        key = lambda row: (row["rule_id"], json.dumps(row["target"], sort_keys=True))
        expected = {key(row) for row in before["findings"] if row["verdict"] == "误报"}
        a = {key(row):(row["state"],row["reason"]) for row in before["evaluation_catalog"]}
        b = {key(row):(row["state"],row["reason"]) for row in after["evaluation_catalog"]}
        differences = {k for k in a.keys() | b.keys() if a.get(k) != b.get(k)}
        assert differences == expected, before["site_id"]
        changed += len(differences)
        directory = GOLD / before["site_id"]
        if before["site_id"] == "all-star-plumbing-fresno":
            report = generate(read_json(directory/"input.json"), directory, private, ruleset="v22_capability_public_v10")[0]
            assert LOCALITY_NOTE == "This organic search sample mostly reflected other areas, so it is not used to judge local visibility."
            assert report.limitations.count(LOCALITY_NOTE) == 1
            assert not any(code in note for note in report.limitations for code in ("sample_not_local", "page_out_of_scope", "/payload/"))
        frozen = ROOT.parent / ".release-private/2026-11-03-gold/p0-10-reports" / (before["site_id"]+".json")
        if frozen.is_file():
            report = generate(read_json(directory/"input.json"), directory, private, ruleset="v22_capability_public_v9")[0]
            assert canonical_json_bytes(report) == canonical_json_bytes(read_json(frozen))
    assert changed == 10
    assert next(c for c in new["cases"] if c["site_id"] == "nova-service")["frozen_replay_byte_identical"] is True


def test_region_only_location_is_not_also_an_unqualified_city():
    row = record("Find Plumbing Repairs in Idaho, United States", url="https://example.test/united-states/idaho/plumbing-repair")
    audit = classify_record(row, Obj(city="Fresno", region="CA", country_code="US"))
    assert audit["state"] == "non_local"
    assert all(s["city"] is None for s in audit["signals"])


def test_organic_gate_does_not_change_maps_present_domains_or_failed_calls(monkeypatch):
    from uuid import UUID
    from app.report_v22 import market_findings as rules
    monkeypatch.setattr(rules, "decision", lambda view, rule, target, state, reason, **kw: (rule, target.result_type, state, reason))
    market = Obj(**vars(MARKET), latitude=30.42, longitude=-87.21)
    context = Obj(target_market=market, site_url="https://client.test/", competitors=[], search_language="en", search_device="mobile")
    records = [Obj(**record(url=f"https://other{i}.test/"), result_type=kind, rank_source=kind)
               for kind in ("organic", "maps") for i in range(3)]
    run = Obj(query="Plumbing near me", results=records, google_call=Obj(status="succeeded"), maps_call=Obj(status="succeeded"))
    source = Obj(binding=Obj(snapshot_id=UUID(int=1)), payload=Obj(target_point=market, query_runs=[run]))
    view = Obj(available=lambda kind: True, context=context, sources={"serp":source}, refs=lambda *a, **k:[], field_refs=lambda *a:[])
    rows = list(rules.evaluate(view, sampling_quality=True))
    assert (rules.DOMAIN, "organic", "not_checked", "sample_not_local") in rows
    assert (rules.DOMAIN, "maps", "triggered", "condition_met") in rows
    records[0].url = "https://client.test/"
    assert (rules.DOMAIN, "organic", "not_triggered", "condition_not_met") in list(rules.evaluate(view, sampling_quality=True))
    records[0].url = None
    assert (rules.DOMAIN, "organic", "not_checked", "identity_unresolved") in list(rules.evaluate(view, sampling_quality=True))
    run.google_call.status = "failed"
    assert (rules.DOMAIN, "organic", "not_checked", "source_ineligible") in list(rules.evaluate(view, sampling_quality=True))


def test_unchecked_admin_page_is_excluded_without_poisoning_title_coverage(monkeypatch):
    from uuid import UUID
    from app.report_v22 import site_findings as rules
    monkeypatch.setattr(rules, "decision", lambda view, rule, target, state, reason, **kw: (rule, state, reason))
    pages = [Obj(url="https://example.test/wp-login.php", final_url=None, check_status="not_checked", status_code=None)]
    for name in ("a", "b"):
        pages.append(Obj(url=f"https://example.test/{name}", final_url=f"https://example.test/{name}", check_status="checked", status_code=200, content_type="text/html", title=name, meta_robots=[]))
    source = Obj(binding=Obj(snapshot_id=UUID(int=1)), payload=Obj(pages=pages))
    view = Obj(available=lambda kind: True, sources={"site":source}, refs=lambda *a, **k:[], field_refs=lambda *a:[])
    old = list(rules.evaluate(view))
    new = list(rules.evaluate(view, sampling_quality=True))
    assert (rules.NOINDEX, "not_checked", "page_out_of_scope") in new
    assert (rules.TITLE, "not_triggered", "condition_not_met") in new
    assert (rules.TITLE, "not_checked", "field_not_observed") in old
    assert [r for r in old if r[0] == rules.HTTP] == [r for r in new if r[0] == rules.HTTP]


@pytest.mark.anyio
async def test_v10_sampling_uses_separate_snapshot_and_selection_checkpoints():
    import fakeredis.aioredis
    from app.jobs_v22.checkpoints import JobCheckpoints
    from test_v22_site_inventory_stage import JOB_ID, RecordingFetcher, InterruptingFirecrawl, stage, prospect_request
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    keys = []
    class RecordingCheckpoints(JobCheckpoints):
        async def run_once(self, job_id, key, operation):
            keys.append(key)
            return await super().run_once(job_id, key, operation)
    checkpoints = RecordingCheckpoints(redis, prefix="p100", ttl_seconds=600)
    firecrawl = InterruptingFirecrawl(); firecrawl.calls = 1
    worker = stage(RecordingFetcher(), firecrawl)
    worker.resource_limits = True
    await worker.collect(job_id=JOB_ID, request=prospect_request(), checkpoints=checkpoints)
    before = {k: await redis.get(k) for k in await redis.keys("*")}
    worker.sampling_quality = True
    await worker.collect(job_id=JOB_ID, request=prospect_request(), checkpoints=checkpoints)
    for version in ("v2", "v3"):
        for kind in ("snapshot", "selection", "manifest"):
            assert any(f"site_inventory_{version}:{kind}:" in k for k in keys)
    assert all([await redis.get(k) == value for k, value in before.items()])
