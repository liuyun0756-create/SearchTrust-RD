"""Offline tests of the complete frozen RPC graph and its trust boundary."""
import json
import asyncio
import gzip
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from app.jobs_v22.digest import request_digest, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.verified_input_resolver import SupabaseVerifiedInputResolver
from app.jobs_v22.verified_models import VerifiedTaskRequest
from app.competitors_v22.selection import analysis_discovery_input_digest
from app.report_v22.models import ReportV22
from findings_helpers import collection, market, site
from test_v22_first_party_findings import trusted, gsc_value, ga4_value

JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
OTHER_ID = "99999999-9999-4999-8999-999999999999"
PUBLIC_ID = "00000000-0000-0000-0000-000000000049"
SECRET = "private-response-must-never-escape"
FIXTURES = Path(__file__).parent / "fixtures/report_v22_golden"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def binding_request(payload):
    sources = {s["source_type"]: s["snapshot_id"] for s in payload["first_party_snapshots"]}
    identity = dict(case_id=payload["case_id"], job_id=str(JOB_ID),
        parent_report_id=payload["parent_report"]["report_version"]["report_id"],
        gsc_snapshot_id=sources["gsc"], ga4_snapshot_id=sources["ga4"], public_gbp_snapshot_id=PUBLIC_ID)
    checksum = verified_request_digest(identity)
    identity.pop("job_id")
    return VerifiedTaskRequest.model_validate_json(json.dumps({**identity, "input_checksum": checksum}))


def resolved_payload():
    parent = json.loads((FIXTURES / "prospect.json").read_text())
    parent["identity"]["business"]["public_gbp_url"] = "https://maps.google.com/?cid=123"
    evidence = deepcopy(parent["evidence_index"][0])
    evidence.update(evidence_id="ev_public_gbp", source_type="gbp", snapshot_id=PUBLIC_ID,
        health_status="healthy", source_locator={"url": "https://maps.google.com/?cid=123", "field_path": "profile.name"})
    parent["evidence_index"].append(evidence)
    for coverage in parent["data_coverage"]["sources"]:
        if coverage["source_type"] == "gbp":
            coverage.update(snapshot_ids=[PUBLIC_ID], health_status="healthy", identity_match_status="matched")
    source_market = market()
    sources = [site([{"status_code": 503, "title": "Broken"},
        {"status_code": 200, "title": "Shared", "meta_robots": ["noindex"]},
        {"status_code": 200, "title": "Shared"}]), source_market, collection(source_market)]
    rows = {}
    for source in sources:
        if source.binding.source_type == "competitor":
            source.payload.job_id = UUID(parent["report_version"]["report_id"])
        rows[source.binding.source_type + "_snapshot"] = dict(
            snapshot_id=str(source.binding.snapshot_id), case_id=parent["identity"]["case_id"],
            source_type=source.binding.source_type, schema_version=source.payload.schema_version,
            normalized_payload=source.payload.model_dump(mode="json"),
            payload_checksum=request_digest(source.payload), created_at="2026-08-30T00:00:00Z",
            fetched_at=source.payload.completed_at.isoformat(), expires_at="2026-08-31T00:00:00Z")
    first_party = [trusted("gsc", gsc_value(), 950).model_dump(mode="json"),
        trusted("ga4", ga4_value(), 951).model_dump(mode="json")]
    for snapshot in first_party:
        snapshot["case_id"] = parent["identity"]["case_id"]
    return dict(schema_version="v22_verified_resolved_input_v1", job_id=str(JOB_ID),
        case_id=parent["identity"]["case_id"], parent_report=parent,
        parent_payload_checksum=verified_request_digest(parent), first_party_snapshots=first_party, **rows)


async def resolve_response(response, *, payload=None, request=None, handler=None, **options):
    payload = payload or resolved_payload()
    request = request or binding_request(payload)
    calls = []
    def respond(sent):
        calls.append(sent)
        result = handler(sent) if handler else response
        if result.is_stream_consumed:
            return httpx.Response(result.status_code, headers=result.headers, stream=httpx.ByteStream(result.content))
        return result
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        result = await SupabaseVerifiedInputResolver(url="https://storage.example/", service_role_key="service-secret",
            http_client=client, **options).resolve(job_id=JOB_ID, request=request, run_generation=1)
    return result, calls


@pytest.mark.anyio
async def test_resolver_posts_only_bound_ids_and_reconstructs_original_market_context():
    payload = resolved_payload()
    result, calls = await resolve_response(httpx.Response(200, json=payload), payload=payload)
    assert len(calls) == 1
    sent = calls[0]
    assert sent.method == "POST"
    assert sent.url.path == "/rest/v1/rpc/resolve_v22_verified_analysis_input"
    assert json.loads(sent.content) == {"p_job_id": str(JOB_ID), "p_case_id": payload["case_id"], "p_run_generation": 1}
    assert sent.headers["authorization"] == "Bearer service-secret"
    assert sent.headers["apikey"] == "service-secret"
    assert sent.extensions["timeout"]["read"] == 20
    assert isinstance(result.parent_report, ReportV22)
    shared = result.shared_market
    assert str(shared.snapshot_id) == payload["serp_snapshot"]["snapshot_id"]
    assert shared.source_job_id == shared.snapshot.job_id
    assert shared.snapshot_checksum == payload["serp_snapshot"]["payload_checksum"]
    assert shared.created_at == result.serp_snapshot.created_at
    assert shared.expires_at == result.serp_snapshot.expires_at
    assert shared.input_digest == analysis_discovery_input_digest(result.analyze_request)


@pytest.mark.anyio
async def test_parent_hash_is_computed_before_defaults_or_whitespace_are_normalized():
    payload = resolved_payload()
    parent = payload["parent_report"]
    parent["case_context"]["primary_service"] = " Plumber "
    parent["case_context"].pop("search_language")
    parent["identity"]["business"]["primary_location"]["latitude"] = 1e-7
    payload["parent_payload_checksum"] = verified_request_digest(parent)
    result, _ = await resolve_response(httpx.Response(200, json=payload), payload=payload)
    assert result.parent_report.case_context.primary_service == "Plumber"
    assert verified_request_digest(result.parent_report.model_dump(mode="json")) != payload["parent_payload_checksum"]


@pytest.mark.anyio
@pytest.mark.parametrize("path,value", [
    (("job_id",), OTHER_ID), (("case_id",), OTHER_ID),
    (("schema_version",), "unknown"), (("parent_payload_checksum",), "sha256:" + "0" * 64),
    (("parent_report", "report_version", "report_id"), OTHER_ID),
    (("site_snapshot", "snapshot_id"), OTHER_ID), (("site_snapshot", "case_id"), OTHER_ID),
    (("site_snapshot", "source_type"), "serp"), (("site_snapshot", "schema_version"), "unknown"),
    (("site_snapshot", "normalized_payload", "canonical_host"), "wrong.test"),
    (("serp_snapshot", "payload_checksum"), "sha256:" + "0" * 64),
    (("serp_snapshot", "expires_at"), "2020-01-01T00:00:00Z"),
    (("competitor_snapshot", "snapshot_id"), OTHER_ID),
    (("first_party_snapshots", 0, "snapshot_id"), OTHER_ID),
    (("first_party_snapshots", 0, "case_id"), OTHER_ID),
    (("first_party_snapshots", 0, "source_type"), "ga4"),
    (("first_party_snapshots", 0, "payload_checksum"), "sha256:" + "0" * 64),
    (("first_party_snapshots", 0, "identity_match_status"), "mismatch"),
    (("first_party_snapshots", 0, "raw_payload"), {"access_token": SECRET}),
])
async def test_resolver_rejects_changed_graph_without_echoing_payload(path, value, caplog):
    payload = resolved_payload()
    request = binding_request(payload)
    node = payload
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(DeterministicJobError) as error:
        await resolve_response(httpx.Response(200, json=payload), request=request)
    assert SECRET not in str(error.value) + caplog.text
    assert error.value.__suppress_context__


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["missing", "duplicate", "url", "health", "coverage", "extra"])
async def test_resolver_checks_public_gbp_binding_even_with_valid_parent_hash(change):
    payload = resolved_payload()
    parent = payload["parent_report"]
    item = parent["evidence_index"][-1]
    if change == "missing": parent["evidence_index"].pop()
    if change == "duplicate":
        extra = {**item, "evidence_id": "ev_other_gbp", "snapshot_id": OTHER_ID}
        parent["evidence_index"].append(extra)
        parent["data_coverage"]["sources"][3]["snapshot_ids"].append(OTHER_ID)
    if change == "url": item["source_locator"]["url"] = "https://wrong.test/"
    if change == "health": item["health_status"] = "unhealthy"
    if change == "coverage": parent["data_coverage"]["sources"][3]["identity_match_status"] = "mismatch"
    if change == "extra": payload["first_party_snapshots"].append(deepcopy(payload["first_party_snapshots"][0]))
    payload["parent_payload_checksum"] = verified_request_digest(parent)
    with pytest.raises(DeterministicJobError):
        await resolve_response(httpx.Response(200, json=payload), payload=payload)


@pytest.mark.anyio
async def test_resolver_rejects_wrong_small_request_checksum():
    payload = resolved_payload()
    request = binding_request(payload).model_copy(update={"input_checksum": "sha256:" + "f" * 64})
    with pytest.raises(DeterministicJobError):
        await resolve_response(httpx.Response(200, json=payload), request=request)


@pytest.mark.anyio
@pytest.mark.parametrize("status,error", [(400, DeterministicJobError), (403, DeterministicJobError),
    (409, DeterministicJobError), (429, TransientJobError), (500, TransientJobError), (503, TransientJobError),
    (302, DeterministicJobError)])
async def test_resolver_http_errors_are_safe_and_redirects_not_followed(status, error, caplog):
    with pytest.raises(error) as raised:
        await resolve_response(httpx.Response(status, text=SECRET, headers={"location": "https://elsewhere.example"}))
    assert SECRET not in str(raised.value) + caplog.text


@pytest.mark.anyio
async def test_resolver_timeout_is_transient_and_suppresses_sensitive_exception():
    def fail(request): raise httpx.ReadTimeout(SECRET, request=request)
    with pytest.raises(TransientJobError) as error:
        await resolve_response(None, handler=fail)
    assert SECRET not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.anyio
@pytest.mark.parametrize("body,headers", [(b"not-json", {}), (b"[]", {}),
    (b"{}", {"content-length": "25000001"}), (b"{}", {"content-length": "bad"}),
    (b"{}", {"content-length": "-1"}), (b"x" * 25_000_001, {}),
    (b"x" * 25_000_001, {"content-length": "2"})])
async def test_resolver_rejects_malformed_or_oversized_body(body, headers):
    with pytest.raises(DeterministicJobError):
        await resolve_response(httpx.Response(200, content=body, headers=headers))


def test_resolved_model_can_build_its_schema_without_importing_adapter_first():
    result = subprocess.run([sys.executable, "-c",
        "from app.jobs_v22.verified_models import VerifiedResolvedInput; VerifiedResolvedInput.model_json_schema()"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["payload_schema", "resource", "coverage", "oauth", "competitor_market"])
async def test_resolver_rejects_inconsistent_nested_payload_even_after_resigning(change):
    payload = resolved_payload()
    snapshot = payload["first_party_snapshots"][0]
    if change == "payload_schema": snapshot["normalized_payload"]["schema_version"] = "unknown"
    if change == "resource": snapshot["external_resource_id"] = "sc-domain:wrong.test"
    if change == "coverage": snapshot["coverage_end"] = "2026-09-06"
    if change == "oauth": snapshot["normalized_payload"]["access_token"] = SECRET
    if change == "competitor_market":
        snapshot = payload["competitor_snapshot"]
        snapshot["normalized_payload"]["market_snapshot_id"] = OTHER_ID
    snapshot["payload_checksum"] = request_digest(snapshot["normalized_payload"])
    with pytest.raises(DeterministicJobError):
        await resolve_response(httpx.Response(200, json=payload), payload=payload)


class CountedStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.reads = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            self.reads += 1
            yield chunk


@pytest.mark.anyio
async def test_resolver_rejects_compression_before_reading_or_decoding(caplog):
    stream = CountedStream([gzip.compress(b" " * 1_000_000)])
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream)
    with pytest.raises(DeterministicJobError):
        await resolve_response(None, handler=handler)
    assert stream.reads == 0
    assert calls[0].headers["accept-encoding"] == "identity"
    assert SECRET not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["trickle", "stall", "headers"])
async def test_resolver_has_total_deadline_for_entire_response(mode, monkeypatch):
    monkeypatch.setattr("app.jobs_v22.verified_input_resolver.TOTAL_TIMEOUT_SECONDS", .02, raising=False)
    payload = resolved_payload()
    stream = CountedStream([b" "] * 10 + [json.dumps(payload).encode()], delay=.01 if mode == "trickle" else .08)
    if mode == "headers":
        async def respond(request):
            await asyncio.sleep(.08)
            return httpx.Response(200, json=payload)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(TransientJobError):
                await SupabaseVerifiedInputResolver(url="https://storage.example", service_role_key="secret", http_client=client).resolve(
                    job_id=JOB_ID, request=binding_request(payload), run_generation=1)
    else:
        with pytest.raises(TransientJobError):
            await resolve_response(httpx.Response(200, stream=stream), payload=payload)
        assert stream.reads < len(stream.chunks)


@pytest.mark.anyio
@pytest.mark.parametrize("path", [(), ("current",), ("previous",), ("current", "totals"),
    ("previous", "queries"), ("current", "totals", "rows", 0)])
async def test_gsc_rejects_all_unknown_nested_fields_before_conversion(path):
    payload = resolved_payload()
    snapshot = payload["first_party_snapshots"][0]
    node = snapshot["normalized_payload"]
    for key in path:
        node = node[key]
    node["arbitrary_unknown_field"] = {"secret": SECRET}
    snapshot["payload_checksum"] = request_digest(snapshot["normalized_payload"])
    with pytest.raises(DeterministicJobError):
        await resolve_response(httpx.Response(200, json=payload), payload=payload)


def test_ga4_and_all_public_payload_model_hierarchies_forbid_extra_fields():
    from app.google_connections_v22.ga4 import Ga4Snapshot
    from app.collectors.site_inventory_models import SiteInventorySnapshot
    from app.collectors.serp_market_models import SerpMarketSnapshot
    from app.competitors_v22.models import CompetitorCollectionSnapshot

    for model in (Ga4Snapshot, SiteInventorySnapshot, SerpMarketSnapshot, CompetitorCollectionSnapshot):
        schema = model.model_json_schema()
        for definition in [schema, *schema.get("$defs", {}).values()]:
            if definition.get("type") == "object":
                assert definition.get("additionalProperties") is False, definition.get("title")


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["device", "language", "country", "latitude", "longitude", "label",
    "competitor_job", "competitor_id", "competitor_domain", "competitor_name"])
async def test_resolver_binds_public_context_and_competitors(change):
    payload = resolved_payload()
    parent = payload["parent_report"]
    context = parent["case_context"]
    if change in ("device", "language"):
        context["search_" + change] = "desktop" if change == "device" else "fr"
    if change in ("country", "latitude", "longitude", "label"):
        key = {"country": "country_code", "latitude": "latitude", "longitude": "longitude", "label": "display_name"}[change]
        value = {"country": "CA", "latitude": 44.0, "longitude": -80.0, "label": "Toronto"}[change]
        context["target_market"][key] = value
        parent["market_snapshot"]["target_market"][key] = value
    competitor = payload["competitor_snapshot"]["normalized_payload"]
    if change == "competitor_job": competitor["job_id"] = OTHER_ID
    if change == "competitor_id": competitor["competitors"][0]["competitor"]["competitor_id"] = "cp_unrelated"
    if change == "competitor_domain": competitor["competitors"][0]["competitor"]["website_url"] = "https://unrelated.test/"
    if change == "competitor_name": competitor["competitors"][0]["competitor"]["business_name"] = "Unrelated"
    payload["competitor_snapshot"]["payload_checksum"] = request_digest(competitor)
    payload["parent_payload_checksum"] = verified_request_digest(parent)
    with pytest.raises(DeterministicJobError):
        await resolve_response(httpx.Response(200, json=payload), payload=payload)
