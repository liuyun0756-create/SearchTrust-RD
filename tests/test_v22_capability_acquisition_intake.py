import json
from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from redis.exceptions import RedisError

from app.core.config import settings
from app.jobs_v22.errors import DeterministicJobError
from test_api_v2_jobs import build_app, RecordingQueue
from test_v22_capability_acquisition_preparation import inputs
from test_v22_capability_dispatch import configure


@pytest.fixture
def anyio_backend():
    return "asyncio"


def scenario():
    args, envelope, prep = inputs()
    saved = dict(business_version_id=str(args["business"].id),request=envelope.model_dump(mode="json"),run_generation=1,status="queued")
    status = dict(job_id=str(args["job_id"]),revision=1,run_generation=1,status="queued",stage="queued",progress=0,
        message="Queued to prepare the confirmed analysis sources.",report=None,error=None,
        created_at=prep["created_at"],deadline_at=prep["deadline_at"],updated_at=prep["created_at"])
    headers = {"Authorization":"Bearer test-internal-token","X-SearchTrust-User-ID":str(args["user_id"]),
        "X-SearchTrust-Job-ID":str(args["job_id"]),"X-SearchTrust-Workflow-ID":str(uuid4()),
        "X-SearchTrust-Discovery-ID":str(envelope.competitor_discovery.discovery_id),"Idempotency-Key":"original-reserved-work"}
    return args,envelope,saved,status,headers


def enable(monkeypatch,runtime,rpc,envelope):
    configure(monkeypatch,runtime,rpc)
    monkeypatch.setattr(settings,"V22_CAPABILITY_INTAKE_ENABLED",True)
    runtime.acquisition_queue=RecordingQueue()
    runtime.discovery_verifier=AsyncMock()
    runtime.discovery_verifier.verify.return_value=envelope.competitor_discovery


@pytest.mark.anyio
async def test_planning_revision_is_forwarded_to_atomic_freeze_but_replay_recovers_original(monkeypatch):
    app,runtime,_,_=build_app(monkeypatch)
    _,envelope,saved,status,headers=scenario();calls=[];recorded=None
    def respond(sent):
        nonlocal recorded
        name=sent.url.path.rsplit("/",1)[-1];body=json.loads(sent.content);calls.append(name)
        if name=="lookup_v22_acquisition_intake":return httpx.Response(200,content=json.dumps(recorded))
        if name=="start_v22_acquisition_intake_with_planning":
            assert body["p_planning_revision"]==2;recorded=saved;return httpx.Response(200,json=saved)
        assert name=="get_v22_acquisition_status";return httpx.Response(200,json=status)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as rpc:
        enable(monkeypatch,runtime,rpc,envelope)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://test") as client:
            for revision in ("2","3"):
                result=await client.post("/api/v2/capability-acquisition",headers={**headers,"X-SearchTrust-Planning-Revision":revision},json=saved["request"]["analyze_request"])
                assert result.status_code==202
    assert calls.count("start_v22_acquisition_intake_with_planning")==1
    await runtime.redis.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("failure",["sql-ack","before-enqueue","after-enqueue"])
async def test_first_intake_and_lost_queue_ack_replay_use_saved_intent_without_reverification(monkeypatch,failure):
    app,runtime,store,formal_queue=build_app(monkeypatch)
    args,envelope,saved,status,headers=scenario()
    recorded=None
    calls=[]
    def respond(sent):
        nonlocal recorded
        name=sent.url.path.rsplit("/",1)[-1]; data=json.loads(sent.content); calls.append(name)
        if name=="lookup_v22_acquisition_intake":
            assert data["p_user_id"]==str(args["user_id"])
            return httpx.Response(200,content=json.dumps(recorded))
        if name=="start_v22_acquisition_intake":
            assert json.loads(data["p_request_canonical"])==envelope.model_dump(mode="json")
            recorded=saved
            if failure=="sql-ack":raise httpx.ReadTimeout("private database address")
            return httpx.Response(200,json=saved)
        assert name=="get_v22_acquisition_status"
        return httpx.Response(200,json=status)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as rpc:
        enable(monkeypatch,runtime,rpc,envelope)
        enqueue=runtime.acquisition_queue.enqueue
        async def broken(*args):
            if failure=="after-enqueue":await enqueue(*args)
            raise RedisError("private redis address")
        runtime.acquisition_queue.enqueue=broken
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://test") as client:
            failed=await client.post("/api/v2/capability-acquisition",headers=headers,json=saved["request"]["analyze_request"])
            assert failed.status_code==503 and "private redis" not in failed.text
            runtime.acquisition_queue.enqueue=enqueue
            # Discovery may have expired or disappeared after the transaction.
            runtime.discovery_verifier.verify.side_effect=AssertionError("must recover saved intent")
            for _ in range(2):
                replay=await client.post("/api/v2/capability-acquisition",headers=headers,json=saved["request"]["analyze_request"])
                assert replay.status_code==202 and replay.json()["status"]=="queued"
                assert "business_version_id" not in replay.text and "competitor_discovery" not in replay.text
        assert runtime.acquisition_queue.calls==[(args["job_id"],1)]
    assert calls.count("start_v22_acquisition_intake")==1
    assert runtime.discovery_verifier.verify.await_count==1
    assert not formal_queue.calls and await store.active_count()==0
    await runtime.redis.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("kind",["disabled","unauthenticated","oversize","body","missing-queue","lookup-denied","discovery-denied"])
async def test_intake_fails_closed_before_creating_tasks_and_hides_private_errors(monkeypatch,kind):
    app,runtime,store,formal_queue=build_app(monkeypatch)
    _,envelope,saved,_,headers=scenario(); calls=[]
    def respond(sent):
        calls.append(sent.url.path.rsplit("/",1)[-1])
        if kind=="lookup-denied":return httpx.Response(400,json={"message":"private owner details"})
        return httpx.Response(200,content="null")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as rpc:
        enable(monkeypatch,runtime,rpc,envelope)
        if kind=="disabled":monkeypatch.setattr(settings,"V22_CAPABILITY_INTAKE_ENABLED",False)
        if kind=="unauthenticated":headers.pop("Authorization")
        if kind=="missing-queue":runtime.acquisition_queue=None
        if kind=="discovery-denied":runtime.discovery_verifier.verify.side_effect=DeterministicJobError("DISCOVERY_EXPIRED","private discovery details")
        body=deepcopy(saved["request"]["analyze_request"])
        if kind in {"body","oversize"}:body["private_extra"]="x"*(140_000 if kind=="oversize" else 1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://test") as client:
            response=await client.post("/api/v2/capability-acquisition",headers=headers,json=body)
            assert response.status_code==dict(disabled=503,unauthenticated=401,oversize=413,body=422,
                **{"missing-queue":503,"lookup-denied":409,"discovery-denied":409})[kind]
            assert "private" not in response.text
    assert "start_v22_acquisition_intake" not in calls
    assert not formal_queue.calls and await store.active_count()==0
    if kind!="discovery-denied":runtime.discovery_verifier.verify.assert_not_awaited()
    await runtime.redis.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("terminal",["succeeded","failed"])
async def test_terminal_replay_never_enqueues_and_uses_safe_sql_status(monkeypatch,terminal):
    app,runtime,store,_=build_app(monkeypatch)
    args,envelope,saved,status,headers=scenario();saved["status"]=terminal
    status.update(status=terminal,stage="completed" if terminal=="succeeded" else "failed",revision=5)
    if terminal=="succeeded":
        status.pop("report");status.pop("error")
        status.update(schema_version="v22_capability_task_status_v1",progress=100,
            report_reference=dict(report_id=str(args["job_id"]),case_id=str(args["request"].case_id),
                schema_version="2.2.2",report_type="prospect",payload_checksum="sha256:"+"a"*64))
    else:
        status["error"]=dict(error_code="ANALYSIS_FAILED",user_message="Check the Case.",retryable=False,
            stage="failed",diagnostic_id=str(args["job_id"]))
    calls=[]
    def respond(sent):
        name=sent.url.path.rsplit("/",1)[-1]; calls.append(name)
        return httpx.Response(200,json=saved if name=="lookup_v22_acquisition_intake" else status)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as rpc:
        enable(monkeypatch,runtime,rpc,envelope)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://test") as client:
            response=await client.post("/api/v2/capability-acquisition",headers=headers,json=saved["request"]["analyze_request"])
            assert response.status_code==202 and response.json()["status"]==terminal
        assert not runtime.acquisition_queue.calls
    assert calls==["lookup_v22_acquisition_intake","get_v22_acquisition_status"]
    runtime.discovery_verifier.verify.assert_not_awaited()
    assert await store.active_count()==0
    await runtime.redis.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("mutation",["request","discovery","generation","status-job","completion-case"])
async def test_invalid_saved_receipt_or_status_fails_safely(monkeypatch,mutation):
    app,runtime,_,_=build_app(monkeypatch)
    args,envelope,saved,status,headers=scenario(); body=deepcopy(saved["request"]["analyze_request"])
    if mutation=="request":saved["request"]["analyze_request"]["primary_service"]="Another service"
    if mutation=="discovery":saved["request"]["competitor_discovery"]["discovery_id"]=str(uuid4())
    if mutation=="generation":status["run_generation"]=2
    if mutation=="status-job":status["job_id"]=str(uuid4())
    if mutation=="completion-case":
        saved["status"]="succeeded";status.pop("report");status.pop("error")
        status.update(schema_version="v22_capability_task_status_v1",status="succeeded",stage="completed",progress=100,
            report_reference=dict(report_id=str(args["job_id"]),case_id=str(uuid4()),schema_version="2.2.2",
                report_type="prospect",payload_checksum="sha256:"+"a"*64))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda sent:httpx.Response(200,json=saved if sent.url.path.endswith("lookup_v22_acquisition_intake") else status))) as rpc:
        enable(monkeypatch,runtime,rpc,envelope)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://test") as client:
            response=await client.post("/api/v2/capability-acquisition",headers=headers,json=body)
            assert response.status_code==503
        if mutation in {"request","discovery","completion-case"}:assert not runtime.acquisition_queue.calls
    runtime.discovery_verifier.verify.assert_not_awaited()
    await runtime.redis.aclose()
