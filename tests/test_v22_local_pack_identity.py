"""Offline CID typing and fourth-smoke shape regressions; no private inputs required."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.collectors.serp_market import SerpMarketCollector, sanitize_serp_payload
from app.collectors.serp_market_requests import build_serp_search_plan
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.keyword_visibility import build_keyword_visibility
from test_p1_01_keyword_visibility import keyword_input
from test_v22_local_pack_urls import collect_with_section
from test_v22_serp_market_collector import FixtureProvider, JOB_ID, NOW, plan


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("data_id,expected", [(None, True), ("0x1:0x7b", True),
    ("0x1:0x7c", False), ("invalid", False)])
async def test_numeric_local_pack_place_id_becomes_cid_only_when_data_id_agrees(data_id, expected):
    item = {"title": "Saved customer", "position": 4, "place_id": "123", "website": "https://customer.example/"}
    if data_id is not None:item["data_id"] = data_id
    raw = deepcopy(item)
    result = await collect_with_section([item])
    assert item == raw
    for run in result.query_runs:
        record = next(r for r in run.results if r.result_type == "local_pack")
        assert record.provider_place_id == (None if expected else "123")
        assert record.provider_cid == ("123" if expected else "124" if data_id == "0x1:0x7c" else None)
        # Provenance: the response checksum still addresses the original field,
        # not a rewritten provider response or a new canonical schema.
        payload = FixtureProvider()
        call = next(c for c in plan().calls if c.call_id == run.google_call.call_id)
        response = await payload.search(call)
        response.payload["local_results"] = [item]
        clean = sanitize_serp_payload(response.payload, "google")
        assert clean["local_results"][0]["place_id"] == "123"
        assert record.response_checksum == request_digest(clean)


@pytest.mark.anyio
async def test_explicit_conflicting_cid_is_not_overwritten():
    result = await collect_with_section([{"title": "Conflict", "position": 1,
        "place_id": "123", "cid": "456", "website": "https://customer.example/"}])
    for run in result.query_runs:
        record = next(r for r in run.results if r.result_type == "local_pack")
        assert record.provider_place_id == "123" and record.provider_cid == "456"


@pytest.mark.anyio
@pytest.mark.parametrize("channel", ["maps", "organic"])
async def test_numeric_place_id_is_unchanged_outside_local_pack(channel):
    result = await collect_with_section([{"title": "Other channel", "position": 1,
        "place_id": "123", "website": "https://other.example/", "link": "https://other.example/"}], result_type=channel)
    for run in result.query_runs:
        record = next(r for r in run.results if r.result_type == channel)
        assert record.provider_place_id == "123" and record.provider_cid is None


@pytest.mark.anyio
async def test_position_only_entries_are_separate_but_named_missing_url_still_skips():
    result = await collect_with_section([{"position": 1}, {"title": "No website", "position": 2}])
    for run in result.query_runs:
        assert "SERP_LOCAL_PACK_UNNAMED_RESULT" in run.limitations
        assert "SERP_LOCAL_PACK_RESULT_SKIPPED" in run.limitations
        local = [r for r in run.results if r.result_type == "local_pack"]
        assert len(local) == 1 and local[0].position == 2 and local[0].url is None


@pytest.mark.anyio
async def test_fourth_smoke_shape_identifies_positions_four_and_three_and_checks_all_words():
    fixture = json.loads((Path(__file__).parent / "fixtures/v22_serp_market/local-pack-bef66e51-redacted.json").read_text())
    _, request = keyword_input()
    gbp = next(s for s in request.evidence_input.sources if s.kind == "public_gbp")
    customer_cid = next(k.value for k in gbp.payload.record.observed_entity_keys if k.kind == "cid")
    rows = deepcopy(fixture["rows"])
    for row in rows:
        for item in row["local_results"]:
            if item.get("place_id") == fixture["customer_cid"]:item["place_id"] = customer_cid
    calls = []
    class Provider(FixtureProvider):
        async def search(self, call):
            calls.append(call)
            response = await super().search(call)
            if call.engine == "google":
                response.payload["local_results"] = next(row["local_results"] for row in rows if row["query"] == call.query)
            return response
    search_plan = build_serp_search_plan(queries=[row["query"] for row in rows], target_point=plan().target_point)
    snapshot = await SerpMarketCollector(provider=Provider(), clock=lambda: NOW).collect(
        job_id=JOB_ID, plan=search_plan, location_resolution_calls=0)
    source = next(s for s in request.evidence_input.sources if s.kind == "serp")
    source.payload = snapshot
    request.evidence_input.context.queries = list(snapshot.queries)
    before = canonical_json_bytes(request)
    module = build_keyword_visibility(request)
    again = build_keyword_visibility(request)
    assert [(r.local_pack.status, r.local_pack.position) for r in module.rows] == [
        ("observed", 4), ("not_detected", None), ("observed", 3)]
    assert module.local_pack.checked_keywords == module.local_pack.total_keywords == 3
    assert module.local_pack.observed_keywords == 2
    assert canonical_json_bytes(module) == canonical_json_bytes(again)
    assert canonical_json_bytes(request) == before
    assert len(calls) == 6  # all FixtureProvider calls; pytest disables sockets
    for run in snapshot.query_runs:
        assert "SERP_LOCAL_PACK_UNNAMED_RESULT" in run.limitations
