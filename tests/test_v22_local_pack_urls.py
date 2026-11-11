"""Bounded URL preservation using redacted saved responses; no provider access."""
from copy import deepcopy
import json

import pytest

from app.collectors.serp_market import SerpMarketCollector, sanitize_serp_payload
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from test_v22_serp_market_collector import FixtureProvider, JOB_ID, NOW, load_fixture, plan


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def collect_with_section(items, *, result_type="local_pack"):
    class Provider(FixtureProvider):
        async def search(self, call):
            response = await super().search(call)
            if result_type == "maps" and call.engine == "google_maps":
                response.payload["local_results"] = deepcopy(items)
            elif result_type != "maps" and call.engine == "google":
                section = "local_results" if result_type == "local_pack" else "organic_results"
                response.payload[section] = deepcopy(items)
            return response

    return await SerpMarketCollector(provider=Provider(), clock=lambda: NOW).collect(
        job_id=JOB_ID, plan=plan(), location_resolution_calls=0,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("archive", [1, 2])
async def test_real_redacted_nested_local_pack_urls_survive_sanitization(archive):
    raw = load_fixture(f"local-pack-nested-redacted-{archive}.json")
    original = deepcopy(raw)
    clean = sanitize_serp_payload(raw, "google")
    expected = {
        item["title"]: item["links"]["website"]
        for item in raw["local_results"]["places"] if "links" in item
    }
    assert len(expected) == 3
    assert raw == original
    assert sanitize_serp_payload(clean, "google") == clean
    assert all(set(item.get("links", {})) <= {"website", "link"} for item in clean["local_results"])
    result = await collect_with_section(raw["local_results"])
    for run in result.query_runs:
        records = {r.display_name: r for r in run.results if r.result_type == "local_pack"}
        for title, url in expected.items():
            assert str(records[title].url) == url
            assert records[title].normalized_domain == url.split("/")[2]
            assert records[title].response_checksum == run.google_call.response_checksum
        assert len(records) == (3 if archive == 1 else 4)
        assert "SERP_LOCAL_PACK_RESULT_SKIPPED" in run.limitations
        assert "SERP_LOCAL_PACK_SECTION_INVALID" not in run.limitations


@pytest.mark.anyio
async def test_existing_flat_local_pack_urls_still_normalize():
    items = load_fixture("google-search.json")["local_results"]
    result = await collect_with_section(items)
    for run in result.query_runs:
        records = [r for r in run.results if r.result_type == "local_pack"]
        assert len(records) == len(items) == 2
        for record, item in zip(records, items, strict=True):
            assert str(record.url) == item["website"]
            assert record.normalized_domain == item["website"].split("/")[2].removeprefix("www.")
        assert "SERP_LOCAL_PACK_RESULT_SKIPPED" not in run.limitations


@pytest.mark.anyio
@pytest.mark.parametrize("result_type", ["local_pack", "maps", "organic"])
async def test_missing_urls_remain_null_and_only_local_pack_adds_result_skipped(result_type):
    result = await collect_with_section([
        {"title": "No website observed", "position": 1,
         "links": {"directions": "https://maps.example/directions", "phone": "redacted"}},
    ], result_type=result_type)
    for run in result.query_runs:
        records = [r for r in run.results if r.result_type == result_type]
        assert len(records) == 1
        assert records[0].url is None and records[0].normalized_domain is None
        limitation = f"SERP_{result_type.upper()}_RESULT_SKIPPED"
        if result_type == "local_pack":
            assert limitation in run.limitations
        else:
            assert limitation not in run.limitations


@pytest.mark.anyio
@pytest.mark.parametrize("first_valid", range(4))
async def test_local_pack_uses_first_safe_url_in_explicit_priority_order(first_valid):
    values = [f"https://choice-{i}.example/path" for i in range(4)]
    # Invalid nonempty earlier values must not mask a usable later URL.
    values[:first_valid] = ["javascript:invalid"] * first_valid
    item = {"title": "URL priority", "position": 1, "website": values[0],
            "links": {"website": values[1], "link": values[3]}, "link": values[2]}
    result = await collect_with_section([item])
    for run in result.query_runs:
        record = next(r for r in run.results if r.result_type == "local_pack")
        assert str(record.url) == values[first_valid]
        assert record.normalized_domain == f"choice-{first_valid}.example"


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", [
    "ftp://invalid.example/", "https://user:password@invalid.example/", "https://[",
    "https://" + "a" * 254 + "/", "https://invalid.example/" + "a" * 2083,
])
async def test_invalid_flat_website_falls_back_to_safe_nested_website(invalid):
    result = await collect_with_section([
        {"title": "Fallback", "website": invalid, "links": {"website": "https://safe.example/"}},
    ])
    assert all(str(next(r for r in run.results if r.result_type == "local_pack").url) == "https://safe.example/"
               for run in result.query_runs)


@pytest.mark.parametrize("links", [None, [], "https://not-an-object.example", 1, True])
def test_non_object_links_are_missing_not_errors(links):
    clean = sanitize_serp_payload({"local_results": [{"title": "No links", "links": links}]}, "google")
    assert clean == {"local_results": [{"title": "No links"}]}


@pytest.mark.parametrize("key", ["website", "link"])
def test_nested_links_are_strictly_bounded_strings_and_whitelisted(key):
    allowed = "https://bounded.example/" + "a" * (2083 - len("https://bounded.example/"))
    raw = {"local_results": [{"title": "Bounds", "links": {
        key: allowed, "phone": "redacted", "directions": "https://maps.example/",
        "arbitrary": {"nested": {"website": "https://no.example/"}},
    }}]}
    original = deepcopy(raw)
    clean = sanitize_serp_payload(raw, "google")
    assert clean["local_results"][0]["links"] == {key: allowed}
    assert len(clean["local_results"][0]["links"][key]) == 2083
    assert raw == original
    for rejected in (allowed + "a", {"url": "https://no.example/"}, [allowed], 123, False, " "):
        raw["local_results"][0]["links"][key] = rejected
        assert "links" not in sanitize_serp_payload(raw, "google")["local_results"][0]


@pytest.mark.anyio
@pytest.mark.parametrize("result_type,expected", [("maps", None), ("organic", "https://flat-link.example/")])
async def test_maps_and_organic_url_selection_and_nested_sanitization_are_unchanged(result_type, expected):
    item = {"title": "Other result type", "website": "javascript:invalid", "link": "https://flat-link.example/",
            "links": {"website": "https://nested.example/"}}
    engine = "google_maps" if result_type == "maps" else "google"
    section = "local_results" if result_type == "maps" else "organic_results"
    assert "links" not in sanitize_serp_payload({section: [item]}, engine)[section][0]
    result = await collect_with_section([item], result_type=result_type)
    for run in result.query_runs:
        record = next(r for r in run.results if r.result_type == result_type)
        assert (str(record.url) if record.url is not None else None) == expected


@pytest.mark.parametrize("fixture,engine,checksum,size", [
    ("google-search.json", "google", "sha256:dc2554bc4fb9bfce10e4297e422aa20e1501d417e5a4084f584bf716080200a9", 675),
    ("google-maps.json", "google_maps", "sha256:f0bca0c415539cff33bef95fd09fc9b916d2a7f8a961ec377639f6f713442ab1", 534),
])
def test_existing_flat_canonical_bytes_and_checksums_match_pre_fix_baseline(fixture, engine, checksum, size):
    raw = load_fixture(fixture)
    before = deepcopy(raw)
    clean = sanitize_serp_payload(raw, engine)
    assert raw == before
    assert len(canonical_json_bytes(clean)) == size
    assert request_digest(clean) == checksum
    assert canonical_json_bytes(sanitize_serp_payload(clean, engine)) == canonical_json_bytes(clean)


@pytest.mark.anyio
async def test_nested_links_survive_both_checkpoint_sanitization_passes_and_reuse_exact_bytes():
    from app.jobs_v22.serp_market_stage import CheckpointedSerpMarketStage
    from test_v22_serp_market_stage import SuccessfulSearchProvider, UnusedLocationProvider, prospect_request, checkpoints

    class Provider(SuccessfulSearchProvider):
        async def search(self, call, **kwargs):
            response = await super().search(call, **kwargs)
            if call.engine == "google":
                response.payload["local_results"] = load_fixture("local-pack-nested-redacted-1.json")["local_results"]
            return response

    provider, saved = Provider(), checkpoints()
    stage = CheckpointedSerpMarketStage(search_provider=provider, location_provider=UnusedLocationProvider(), clock=lambda: NOW)
    try:
        result = await stage.collect(job_id=JOB_ID, request=prospect_request(), checkpoints=saved)
        saved_before = {key: await saved.redis.get(key) async for key in saved.redis.scan_iter()}
        repeated = await stage.collect(job_id=JOB_ID, request=prospect_request(), checkpoints=saved)
        assert canonical_json_bytes(result) == canonical_json_bytes(repeated)
        assert provider.network_calls == 6  # Fake provider only; no HTTP.
        assert all(sum(r.result_type == "local_pack" and r.normalized_domain is not None for r in run.results) == 3
                   for run in result.query_runs)
        seen_nested_checkpoints = 0
        for key, before in saved_before.items():
            assert await saved.redis.get(key) == before
            raw = json.loads(before)
            if raw.get("schema_version") == "serp_provider_response_v1":
                assert raw["payload_checksum"] == request_digest(raw["payload"])
                assert sanitize_serp_payload(raw["payload"], raw["engine"]) == raw["payload"]
                if raw["engine"] == "google":
                    assert sum("links" in item for item in raw["payload"]["local_results"]) == 3
                    seen_nested_checkpoints += 1
        assert seen_nested_checkpoints == 3
    finally:
        await saved.redis.aclose()
