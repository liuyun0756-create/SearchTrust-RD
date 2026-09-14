import json
from copy import deepcopy

import httpx
import pytest

from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.verified_result_persistence import SupabaseVerifiedResultPersister
from app.jobs_v22.verified_models import VerifiedResolvedInput
from app.report_v22.models import ReportV22
from test_v22_verified_input_resolver import (JOB_ID, OTHER_ID, PUBLIC_ID, SECRET, FIXTURES,
    resolved_payload, binding_request)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def persistence_inputs():
    payload = resolved_payload()
    bound = VerifiedResolvedInput.model_validate_json(json.dumps(payload))
    report = json.loads((FIXTURES / "verified.json").read_text())
    report["report_version"]["report_id"] = str(JOB_ID)
    report["identity"] = deepcopy(payload["parent_report"]["identity"])
    report["evidence_index"].append(deepcopy(payload["parent_report"]["evidence_index"][-1]))
    for coverage in report["data_coverage"]["sources"]:
        if coverage["source_type"] == "gbp":
            coverage.update(snapshot_ids=[PUBLIC_ID], health_status="healthy", identity_match_status="matched")
    return binding_request(payload), bound, ReportV22.model_validate_json(json.dumps(report))


async def persist_response(response, *, values=None, handler=None):
    request, bound, report = values or persistence_inputs()
    calls = []
    def respond(sent):
        calls.append(sent)
        return handler(sent) if handler else response
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        await SupabaseVerifiedResultPersister(url="https://storage.example/", service_role_key="service-secret",
            http_client=client).persist(job_id=JOB_ID, case_id=request.case_id, run_generation=1,
                request=request, resolved_input=bound, report=report)
    return calls


@pytest.mark.anyio
@pytest.mark.parametrize("idempotent", [False, True])
async def test_persister_validates_and_sends_only_exact_verified_rpc(idempotent):
    values = persistence_inputs()
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
    request, bound, report = persistence_inputs()
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
    if change == "bound": bound.case_id = __import__("uuid").UUID(OTHER_ID)
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
