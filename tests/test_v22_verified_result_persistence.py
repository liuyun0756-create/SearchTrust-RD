import json
import gzip
from copy import copy, deepcopy

import httpx
import pytest

from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.verified_result_persistence import SupabaseVerifiedResultPersister
from app.jobs_v22.verified_models import VerifiedResolvedInput
from app.report_v22.models import ReportV22
from test_v22_verified_input_resolver import (JOB_ID, OTHER_ID, PUBLIC_ID, SECRET, FIXTURES,
    resolved_payload, binding_request, CountedStream, resolve_response)


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def persistence_inputs():
    payload = resolved_payload()
    bound, _ = await resolve_response(httpx.Response(200, json=payload), payload=payload)
    report = json.loads((FIXTURES / "verified.json").read_text())
    report["report_version"]["report_id"] = str(JOB_ID)
    report["identity"] = deepcopy(payload["parent_report"]["identity"])
    report["evidence_index"].append(deepcopy(payload["parent_report"]["evidence_index"][-1]))
    for coverage in report["data_coverage"]["sources"]:
        if coverage["source_type"] == "gbp":
            coverage.update(snapshot_ids=[PUBLIC_ID], health_status="healthy", identity_match_status="matched")
    return binding_request(payload), bound, ReportV22.model_validate_json(json.dumps(report))


async def persist_response(response, *, values=None, handler=None):
    request, bound, report = values or await persistence_inputs()
    calls = []
    def respond(sent):
        calls.append(sent)
        result = handler(sent) if handler else response
        if result.is_stream_consumed:
            return httpx.Response(result.status_code, headers=result.headers, stream=httpx.ByteStream(result.content))
        return result
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        await SupabaseVerifiedResultPersister(url="https://storage.example/", service_role_key="service-secret",
            http_client=client).persist(job_id=JOB_ID, case_id=request.case_id, run_generation=1,
                request=request, resolved_input=bound, report=report)
    return calls


@pytest.mark.anyio
@pytest.mark.parametrize("idempotent", [False, True])
async def test_persister_validates_and_sends_only_exact_verified_rpc(idempotent):
    values = await persistence_inputs()
    calls = await persist_response(httpx.Response(200, json=[{"report_id": str(JOB_ID), "idempotent": idempotent}]), values=values)
    assert len(calls) == 1
    sent = calls[0]
    assert sent.method == "POST"
    assert sent.url.path == "/rest/v1/rpc/persist_v22_verified_result"
    assert sent.headers["authorization"] == "Bearer service-secret"
    assert sent.headers["apikey"] == "service-secret"
    assert json.loads(sent.content) == dict(p_job_id=str(JOB_ID), p_case_id=str(values[0].case_id),
        p_report_payload=values[2].model_dump(mode="json"), p_run_generation=1)


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["schema", "job", "case", "parent", "version", "gsc", "ga4", "gbp",
    "evidence", "source_swap", "coverage", "coverage_missing", "public_url", "constructed", "request", "bound", "bound_checksum"])
async def test_persister_rejects_invalid_or_unbound_reports_before_network(change):
    request, bound, report = await persistence_inputs()
    if change == "schema": report.top_actions = []
    if change == "job": report.report_version.report_id = __import__("uuid").UUID(OTHER_ID)
    if change == "case": report.identity.case_id = __import__("uuid").UUID(OTHER_ID)
    if change == "parent": report.report_version.parent_report_id = __import__("uuid").UUID(OTHER_ID)
    if change == "version": report.report_version.version_number += 1
    if change in ("gsc", "ga4", "gbp"):
        getattr(report.first_party_performance, change).snapshot_id = __import__("uuid").UUID(OTHER_ID)
    if change == "evidence": report.evidence_index[0].snapshot_id = __import__("uuid").UUID(OTHER_ID)
    if change == "source_swap": report.evidence_index[0].source_type = "ga4"
    if change == "coverage": report.data_coverage.sources[0].snapshot_ids = []
    if change == "coverage_missing": report.data_coverage.sources.pop(0)
    if change == "public_url": report.evidence_index[-1].source_locator.url = "https://wrong.test/"
    if change == "constructed": report = ReportV22.model_construct()
    if change == "request": request = request.model_copy(update={"input_checksum": "sha256:" + "f" * 64})
    if change == "bound": bound.payload.case_id = __import__("uuid").UUID(OTHER_ID)
    if change == "bound_checksum": bound.site_snapshot.payload_checksum = "sha256:" + "f" * 64
    def unexpected(_): pytest.fail("invalid report reached network")
    with pytest.raises(DeterministicJobError):
        await persist_response(None, values=(request, bound, report), handler=unexpected)


@pytest.mark.anyio
@pytest.mark.parametrize("status,error", [(400, DeterministicJobError), (403, DeterministicJobError),
    (409, DeterministicJobError), (429, TransientJobError), (500, TransientJobError), (302, DeterministicJobError)])
async def test_persister_http_failure_is_classified_without_body_or_redirect(status, error, caplog):
    with pytest.raises(error) as raised:
        await persist_response(httpx.Response(status, text=SECRET, headers={"location": "https://elsewhere.example"}))
    assert SECRET not in str(raised.value) + caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("response", [httpx.Response(200, text=SECRET), httpx.Response(200, json=[]),
    httpx.Response(200, json=[{"report_id": OTHER_ID, "idempotent": False}]),
    httpx.Response(200, json=[{"report_id": str(JOB_ID)}]),
    httpx.Response(200, json=[{"report_id": str(JOB_ID), "idempotent": "true"}]),
    httpx.Response(200, json=[{"report_id": str(JOB_ID), "idempotent": False}] * 2),
    httpx.Response(200, content=b"x" * 65_537),
    httpx.Response(200, content=b"{}", headers={"content-length": "65537"}),
    httpx.Response(200, content=b"{}", headers={"content-length": "bad"})])
async def test_persister_invalid_ack_is_retryable_and_safe(response, caplog):
    with pytest.raises(TransientJobError) as raised:
        await persist_response(response)
    assert SECRET not in str(raised.value) + caplog.text
    assert raised.value.__suppress_context__


@pytest.mark.anyio
async def test_persister_timeout_is_retryable_and_safe():
    def fail(request): raise httpx.ReadTimeout(SECRET, request=request)
    with pytest.raises(TransientJobError) as raised:
        await persist_response(None, handler=fail)
    assert raised.value.__suppress_context__


@pytest.mark.anyio
async def test_persister_rejects_compression_before_reading_or_decoding():
    stream = CountedStream([gzip.compress(b" " * 1_000_000)])
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream)
    with pytest.raises(TransientJobError):
        await persist_response(None, handler=handler)
    assert stream.reads == 0
    assert calls[0].headers["accept-encoding"] == "identity"


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["trickle", "stall"])
async def test_persister_has_total_deadline_for_body(mode, monkeypatch):
    monkeypatch.setattr("app.jobs_v22.verified_input_resolver.TOTAL_TIMEOUT_SECONDS", .02, raising=False)
    body = json.dumps([{"report_id": str(JOB_ID), "idempotent": False}]).encode()
    stream = CountedStream([b" "] * 10 + [body], delay=.01 if mode == "trickle" else .08)
    with pytest.raises(TransientJobError):
        await persist_response(httpx.Response(200, stream=stream))
    assert stream.reads < len(stream.chunks)


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["unknown", "statement", "fingerprint", "duplicate", "report"])
async def test_persister_binds_every_previous_finding_to_frozen_parent(change):
    from app.report_v22.models import PreviousFindingReference
    from app.report_v22.version_diff_identity import finding_fingerprint

    request, bound, report = await persistence_inputs()
    finding = bound.parent_report.findings[0]
    entry = report.version_diff.entries[0]
    entry.change_type = "confirmed"
    entry.previous_finding = PreviousFindingReference(report_id=request.parent_report_id,
        finding_id=finding.finding_id, statement=finding.statement, fingerprint=finding_fingerprint(finding))
    if change == "unknown": entry.previous_finding.finding_id = "fn_invented"
    if change == "statement": entry.previous_finding.statement = "Invented parent finding"
    if change == "fingerprint": entry.previous_finding.fingerprint = "sha256:" + "f" * 64
    if change == "duplicate": report.version_diff.entries.append(entry.model_copy(deep=True))
    if change == "report": entry.previous_finding.report_id = __import__("uuid").UUID(OTHER_ID)
    def unexpected(_): pytest.fail("invalid previous reference reached network")
    with pytest.raises(DeterministicJobError):
        await persist_response(None, values=(request, bound, report), handler=unexpected)


@pytest.mark.anyio
async def test_persister_accepts_exact_previous_reference():
    from app.report_v22.models import PreviousFindingReference
    from app.report_v22.version_diff_identity import finding_fingerprint

    request, bound, report = await persistence_inputs()
    finding = bound.parent_report.findings[0]
    report.version_diff.entries[0].change_type = "confirmed"
    report.version_diff.entries[0].previous_finding = PreviousFindingReference(report_id=request.parent_report_id,
        finding_id=finding.finding_id, statement=finding.statement, fingerprint=finding_fingerprint(finding))
    calls = await persist_response(httpx.Response(200, json=[{"report_id": str(JOB_ID), "idempotent": False}]),
        values=(request, bound, report))
    assert len(calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["statement", "valid_model_copy", "reseal_rpc"])
async def test_persister_rejects_valid_shaped_parent_mutation(change):
    request, bound, report = await persistence_inputs()
    if change == "reseal_rpc":
        payload = resolved_payload()
        payload["_parent_integrity_seal"] = "sha256:" + "f" * 64
        with pytest.raises(ValueError):
            VerifiedResolvedInput.model_validate_json(json.dumps(payload))
        return
    if change == "valid_model_copy": bound = bound.model_copy(deep=True)
    bound.parent_report.findings[0].statement = "A different but schema-valid parent statement"
    def unexpected(_): pytest.fail("mutated parent reached network")
    with pytest.raises(DeterministicJobError):
        await persist_response(None, values=(request, bound, report), handler=unexpected)


@pytest.mark.anyio
async def test_model_post_init_cannot_recertify_the_resolver_capability():
    request, bound, report = await persistence_inputs()
    bound.parent_report.findings[0].statement = "A different parent statement"
    bound.model_post_init(None)
    def unexpected(_): pytest.fail("post-init recertified a mutated parent")
    with pytest.raises(DeterministicJobError):
        await persist_response(None, values=(request, bound, report), handler=unexpected)


@pytest.mark.anyio
async def test_resolver_seal_is_separate_from_raw_frontend_hash_during_persistence():
    from test_v22_verified_input_resolver import resolve_response
    from app.jobs_v22.digest import verified_request_digest

    payload = resolved_payload()
    payload["parent_report"]["case_context"]["primary_service"] = " Plumber "
    payload["parent_report"]["case_context"].pop("search_language")
    payload["parent_payload_checksum"] = verified_request_digest(payload["parent_report"])
    bound, _ = await resolve_response(httpx.Response(200, json=payload), payload=payload)
    assert verified_request_digest(bound.parent_report.model_dump(mode="json")) != bound.parent_payload_checksum
    request, _, report = await persistence_inputs()
    calls = await persist_response(httpx.Response(200, json=[{"report_id": str(JOB_ID), "idempotent": False}]),
        values=(request, bound, report))
    assert len(calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("probe", ["copy_update_seal", "dump_revalidate", "clear_then_post_init"])
async def test_normal_model_apis_cannot_recertify_a_mutated_parent(probe):
    from app.jobs_v22.digest import request_digest

    request, original, report = await persistence_inputs()
    forged = original.model_copy(deep=True)
    forged.parent_report.findings[0].statement = "A changed but valid parent Finding"
    if probe == "copy_update_seal":
        forged = forged.model_copy(update={"_parent_integrity_seal": request_digest(forged.parent_report)})
    if probe == "dump_revalidate":
        forged = VerifiedResolvedInput.model_validate(forged.model_dump(mode="python"))
    if probe == "clear_then_post_init":
        forged.__pydantic_private__ = {"_parent_integrity_seal": None}
        forged.model_post_init(None)
    def unexpected(_): pytest.fail("forged provenance reached persistence HTTP")
    with pytest.raises(DeterministicJobError):
        await persist_response(None, values=(request, forged, report), handler=unexpected)


@pytest.mark.anyio
@pytest.mark.parametrize("copy_kind", ["copy", "deepcopy", "model_copy", "model_validate", "wrapper_construct", "dataclass_replace"])
async def test_only_the_exact_resolver_instance_is_trusted_even_without_mutation(copy_kind):
    request, original, report = await persistence_inputs()
    if copy_kind == "copy": forged = copy(original)
    if copy_kind == "deepcopy": forged = deepcopy(original)
    if copy_kind == "model_copy": forged = original.model_copy(deep=True)
    if copy_kind == "model_validate": forged = VerifiedResolvedInput.model_validate(original.model_dump(mode="python"))
    if copy_kind == "wrapper_construct": forged = type(original)(original.payload)
    if copy_kind == "dataclass_replace":
        from dataclasses import replace
        forged = replace(original)
    def unexpected(_): pytest.fail("unregistered copy reached persistence HTTP")
    with pytest.raises(DeterministicJobError):
        await persist_response(None, values=(request, forged, report), handler=unexpected)
