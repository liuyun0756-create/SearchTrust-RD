import json
from uuid import UUID

import httpx
import pytest

from app.jobs_v22.capability_result_persistence import SupabaseCapabilityResultPersister
from app.jobs_v22.digest import verified_canonical_json_bytes, verified_request_digest, request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.capability_service import build_service_capability_report
from test_v22_capability_public import inputs as public_inputs
from test_v22_capability_service import inputs as service_inputs


@pytest.fixture
def anyio_backend(): return "asyncio"


def values(mode="first_service"):
    if mode == "acquisition":
        manifest, request = public_inputs(records=[{"title": "Plumbing"}])
        report = build_public_capability_report(manifest, request)
    else:
        manifest, request, parent = service_inputs(mode=mode)
        report = build_service_capability_report(manifest, request, previous_report=parent)
    return dict(user_id=manifest.user_id, case_id=manifest.case_id, job_id=manifest.job_id,
        run_generation=1, manifest=manifest, report=report)


def checksum(report): return verified_request_digest(report.model_dump(mode="json"))


async def persist(response, *, params=None, handler=None):
    calls = []
    def respond(request):
        calls.append(request)
        result = handler(request) if handler else response
        return httpx.Response(result.status_code, headers=result.headers, stream=httpx.ByteStream(result.content))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        receipt = await SupabaseCapabilityResultPersister(url="https://storage.example/", service_role_key="test-key",
            http_client=client).persist(**(params or values()))
    return receipt, calls


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["acquisition", "first_service", "after_acquisition"])
@pytest.mark.parametrize("replay", [False, True])
async def test_exact_result_and_frozen_manifest_complete_in_one_call(mode, replay):
    params = values(mode)
    report = params["report"]
    expected = dict(report_id=str(report.id), payload_checksum=checksum(report), idempotent=replay)
    receipt, calls = await persist(httpx.Response(200, json=[expected]), params=params)
    assert receipt.report_id == report.id and receipt.idempotent is replay
    assert receipt.payload_checksum == checksum(report)
    assert len(calls) == 1
    assert calls[0].url.path == "/rest/v1/rpc/complete_v22_capability_analysis"
    payload = json.loads(calls[0].content)
    assert payload["p_payload"] == report.model_dump(mode="json")
    assert payload["p_canonical_content"].encode() == verified_canonical_json_bytes(report.model_dump(mode="json"))
    assert payload["p_run_generation"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["owner", "case", "job", "generation", "boolean", "manifest", "result", "constructed"])
async def test_invalid_inputs_never_reach_storage(change):
    params = values()
    if change in ("owner", "case", "job"):
        params[{"owner": "user_id", "case": "case_id", "job": "job_id"}[change]] = UUID(int=999999)
    if change == "generation": params["run_generation"] = 0
    if change == "boolean": params["run_generation"] = True
    if change == "manifest": params["manifest"] = {**params["manifest"].model_dump(mode="json"), "ruleset_version": "changed"}
    if change == "result": params["report"].findings[0].evidence_ids = ["invented"]
    if change == "constructed": params["report"] = type(params["report"]).model_construct()
    def unexpected(_): pytest.fail("Invalid result reached a persistence request")
    with pytest.raises(DeterministicJobError) as error:
        await persist(None, params=params, handler=unexpected)
    assert error.value.error_code == "V22_CAPABILITY_PERSISTENCE_INPUT_INVALID"


@pytest.mark.anyio
@pytest.mark.parametrize("code,kind", [(400, DeterministicJobError), (403, DeterministicJobError),
    (409, DeterministicJobError), (429, TransientJobError), (500, TransientJobError), (302, DeterministicJobError)])
async def test_transport_failures_are_bounded_and_do_not_log_private_bodies(code, kind, caplog):
    secret = "private-result-body-must-not-leak"
    with pytest.raises(kind) as error:
        await persist(httpx.Response(code, text=secret, headers={"location": "https://elsewhere.example"}))
    assert secret not in str(error.value) + caplog.text
    assert error.value.__suppress_context__


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["empty", "many", "job", "checksum", "idempotent", "extra", "json", "oversized"])
async def test_bad_ack_is_retryable_not_a_reason_to_refund(change):
    params = values()
    row = dict(report_id=str(params["job_id"]), payload_checksum=checksum(params["report"]), idempotent=False)
    if change in ("job", "checksum", "idempotent", "extra"):
        row[{"job": "report_id", "checksum": "payload_checksum", "idempotent": "idempotent", "extra": "unexpected"}[change]] = "wrong"
    response = httpx.Response(200, json=[] if change == "empty" else [row, row] if change == "many" else [row])
    if change == "json": response = httpx.Response(200, text="not-json")
    if change == "oversized": response = httpx.Response(200, content=b"x" * 65_537)
    with pytest.raises(TransientJobError): await persist(response, params=params)


@pytest.mark.anyio
async def test_lost_response_retries_exact_payload_without_regeneration():
    params = values("acquisition")
    bodies = []
    def respond(request):
        bodies.append(request.content)
        if len(bodies) == 1: raise httpx.ReadTimeout("ack lost")
        return httpx.Response(200, json=[dict(report_id=str(params["job_id"]),
            payload_checksum=checksum(params["report"]), idempotent=True)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        persister = SupabaseCapabilityResultPersister(url="https://storage.example/", service_role_key="test-key", http_client=client)
        with pytest.raises(TransientJobError): await persister.persist(**params)
        receipt = await persister.persist(**params)
    assert receipt.idempotent and bodies[0] == bodies[1]


@pytest.mark.anyio
@pytest.mark.parametrize("service", [False, True])
async def test_checkpoint_generation_and_completion_recovery_use_one_saved_result(service, monkeypatch):
    import fakeredis.aioredis
    from app.jobs_v22.checkpoints import JobCheckpoints
    from app.jobs_v22.capability_public_stage import CheckpointedCapabilityPublicStage
    from app.jobs_v22.capability_service_stage import CheckpointedCapabilityServiceStage
    import app.jobs_v22.capability_public_stage as public_module
    import app.jobs_v22.capability_service_stage as service_module

    if service:
        manifest, request, _ = service_inputs()
        stage, module, builder_name = CheckpointedCapabilityServiceStage(), service_module, "build_service_capability_report"
    else:
        manifest, request = public_inputs([{"title": "Plumbing"}])
        stage, module, builder_name = CheckpointedCapabilityPublicStage(), public_module, "build_public_capability_report"
    original, runs, requests = getattr(module, builder_name), [], []
    def generate(*args, **kwargs):
        runs.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, builder_name, generate)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints = JobCheckpoints(redis, prefix="completion-recovery-test", ttl_seconds=600)
    async def build(): return await stage.build(job_id=manifest.job_id, manifest=manifest, request=request, checkpoints=checkpoints)
    def respond(sent):
        requests.append(sent.content)
        if len(requests) == 1: raise httpx.ReadTimeout("ack lost after commit")
        return httpx.Response(200, json=[dict(report_id=str(manifest.job_id), idempotent=True,
            payload_checksum=verified_request_digest(json.loads(sent.content)["p_payload"]))])
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            persister = SupabaseCapabilityResultPersister(url="https://storage.example/", service_role_key="test-key", http_client=client)
            params = dict(user_id=manifest.user_id, case_id=manifest.case_id, job_id=manifest.job_id, manifest=manifest, run_generation=1)
            with pytest.raises(TransientJobError): await persister.persist(**params, report=await build())
            receipt = await persister.persist(**params, report=await build())
        assert receipt.idempotent and len(runs) == 1 and len(requests) == 2 and requests[0] == requests[1]
    finally:
        await redis.aclose()


@pytest.mark.anyio
async def test_integral_float_uses_the_same_report_checksum_as_javascript_not_legacy_checkpoint_bytes():
    params=values()
    params["report"].evidence[0].value=1.0
    report=params["report"]
    assert request_digest(report)!=checksum(report)
    receipt,calls=await persist(httpx.Response(200,json=[dict(report_id=str(report.id),idempotent=False,payload_checksum=checksum(report))]),params=params)
    canonical=json.loads(calls[0].content)["p_canonical_content"]
    assert '"value":1.0' not in canonical and '"value":1' in canonical
    assert receipt.payload_checksum==checksum(report)
