from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from redis.exceptions import RedisError

from app.core.config import settings
from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository
from app.jobs_v22.capability_runtime_models import CapabilityRequestEnvelope
from test_api_v2_jobs import build_app
from test_v22_capability_input_repository import graph


@pytest.fixture
def anyio_backend():
    return "asyncio"


def receipt():
    m, _, _, task, _ = graph()
    now = datetime.now(timezone.utc)
    envelope = CapabilityRequestEnvelope(schema_version="v22_capability_request_envelope_v1", capability_request=task)
    return dict(
        schema_version="v22_capability_dispatch_v1", user_id=str(m.user_id),
        job_id=str(m.job_id), case_id=str(m.case_id), idempotency_key="same-reserved-task",
        request=envelope.model_dump(mode="json"), run_generation=1, revision=1,
        attempt_count=0, cost_counters={}, status="queued",
        created_at=(now - timedelta(minutes=2)).isoformat(),
        deadline_at=(now + timedelta(minutes=18)).isoformat(), completion=None,
    )


def configure(monkeypatch, runtime, rpc_client, enabled=True):
    monkeypatch.setattr(settings, "V22_CAPABILITY_EXECUTION_ENABLED", enabled)
    monkeypatch.setattr(settings, "V22_SUPABASE_URL", "https://storage.example")
    monkeypatch.setattr(settings, "V22_SUPABASE_SERVICE_ROLE_KEY", SecretStr("test-key"))
    monkeypatch.setattr(settings, "V22_CALLBACK_URL", "https://app.example/callback")
    monkeypatch.setattr(settings, "V22_CALLBACK_SECRET", SecretStr("test-key"))
    runtime.capability_repository = SupabaseCapabilityInputRepository(
        url="https://storage.example", service_role_key="test-key", http_client=rpc_client)


def headers(r):
    return {"Authorization": "Bearer test-internal-token", "X-SearchTrust-User-ID": r["user_id"],
            "X-SearchTrust-Job-ID": r["job_id"], "Idempotency-Key": r["idempotency_key"]}


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["before_enqueue", "after_enqueue"])
async def test_dispatch_replay_repairs_queue_gap_with_same_physical_identity_and_no_credit_call(monkeypatch, failure):
    app, runtime, store, queue = build_app(monkeypatch)
    r = receipt()
    calls = []
    def respond(sent):
        calls.append(sent.url.path)
        return httpx.Response(200, json=r)
    original = queue.enqueue
    async def broken(*args):
        if failure == "after_enqueue":
            await original(*args)
        raise RedisError("private network details")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as rpc:
        configure(monkeypatch, runtime, rpc)
        runtime.queue.enqueue = broken
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            failed = await client.post("/api/v2/capability-analyze", headers=headers(r), json=r["request"])
            assert failed.status_code == 503 and "private network" not in failed.text
            runtime.queue.enqueue = original
            replay = await client.post("/api/v2/capability-analyze", headers=headers(r), json=r["request"])
            assert replay.status_code == 202 and replay.json()["status"] == "queued"
    from uuid import UUID
    state = await store.require_state(UUID(r["job_id"]))
    assert state.created_at.isoformat() == r["created_at"]
    assert state.deadline_at.isoformat() == r["deadline_at"]
    assert state.run_generation == 1 and state.attempt_count == 0
    assert len(set(queue.calls)) == 1
    assert all(path.endswith("/get_v22_capability_dispatch") for path in calls)
    assert await store.get_request(state.job_id) == r["request"]
    await runtime.redis.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["owner", "job", "case", "idem", "envelope", "completion", "failed", "expired"])
async def test_invalid_authorization_never_creates_redis_task_or_enqueues(monkeypatch, mutation):
    app, runtime, store, queue = build_app(monkeypatch)
    original = receipt()
    r = deepcopy(original)
    field = {"owner":"user_id", "job":"job_id", "case":"case_id", "idem":"idempotency_key"}.get(mutation)
    if field:
        r[field] = str(uuid4())
    elif mutation == "envelope":
        r["request"]["capability_request"]["input_checksum"] = "sha256:" + "b" * 64
    elif mutation == "completion":
        r["status"] = "succeeded"
    elif mutation == "failed":
        r["status"] = "failed"
    else:
        r["created_at"] = (datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
        r["deadline_at"] = (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=r))) as rpc:
        configure(monkeypatch, runtime, rpc)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post("/api/v2/capability-analyze", headers=headers(original), json=original["request"])
            assert response.status_code in (409,503)
    assert await store.active_count() == 0 and not queue.calls
    await runtime.redis.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["disabled", "unauthenticated", "body", "oversize", "missing_repository"])
async def test_readiness_auth_and_body_limits_precede_dispatch(monkeypatch, kind):
    app, runtime, store, queue = build_app(monkeypatch)
    r = receipt()
    calls = []
    def respond(sent):
        calls.append(sent)
        return httpx.Response(200, json=r)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as rpc:
        configure(monkeypatch, runtime, rpc, kind != "disabled")
        if kind == "missing_repository":
            runtime.capability_repository = None
        h = headers(r)
        if kind == "unauthenticated":
            h.pop("Authorization")
        body = deepcopy(r["request"])
        if kind in ("body", "oversize"):
            body["raw_payload"] = "private-value" * (1000 if kind == "oversize" else 1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post("/api/v2/capability-analyze", headers=h, json=body)
            assert response.status_code == {"disabled":503,"unauthenticated":401,"body":422,"oversize":413,"missing_repository":503}[kind]
            assert "private-value" not in response.text
    assert not calls and not queue.calls and await store.active_count() == 0
    await runtime.redis.aclose()


@pytest.mark.anyio
async def test_sql_completed_result_returned_without_redis_and_active_recovery_preserves_budgets(monkeypatch):
    app, runtime, store, queue = build_app(monkeypatch)
    r = receipt()
    r.update(run_generation=3,revision=14,attempt_count=2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=r))) as rpc:
        configure(monkeypatch, runtime, rpc)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            first = await client.post("/api/v2/capability-analyze", headers=headers(r), json=r["request"])
            assert first.status_code == 202
            from uuid import UUID
            state = await store.require_state(UUID(r["job_id"]))
            assert (state.run_generation,state.revision,state.attempt_count) == (3,14,2)
            r["status"] = "succeeded"
            r["completion"] = dict(
                schema_version="v22_capability_task_status_v1",job_id=r["job_id"],revision=15,run_generation=3,
                status="succeeded",stage="completed",progress=100,message="Analysis complete.",
                report_reference=dict(report_id=r["job_id"],case_id=r["case_id"],schema_version="2.2.2",
                                      report_type="prospect",payload_checksum="sha256:"+"b"*64),
                created_at=r["created_at"],deadline_at=r["deadline_at"],updated_at=datetime.now(timezone.utc).isoformat())
            monkeypatch.setattr(store, "register_job", AsyncMock(side_effect=RedisError("lost")))
            replay = await client.post("/api/v2/capability-analyze", headers=headers(r), json=r["request"])
            from app.jobs_v22.capability_runtime_models import CapabilityTaskStatusResponse
            assert replay.status_code == 202
            assert replay.json() == CapabilityTaskStatusResponse.model_validate_json(__import__("json").dumps(r["completion"])).model_dump(mode="json")
            assert len(queue.calls) == 1
    await runtime.redis.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["cost-ledger-lost", "attempts-exhausted"])
async def test_dispatch_cannot_reset_spent_cost_controls_or_attempt_budget(monkeypatch, kind):
    app, runtime, store, queue = build_app(monkeypatch)
    r = receipt()
    if kind == "cost-ledger-lost":
        from app.jobs_v22.cost_models import ALLOWED_COUNTER_KEYS
        r["cost_counters"] = {**dict.fromkeys(ALLOWED_COUNTER_KEYS, 0), "cost_schema_version": 1, "provider_attempts_total": 1}
    else:
        r["attempt_count"] = settings.V22_JOB_MAX_ATTEMPTS
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=r))) as rpc:
        configure(monkeypatch, runtime, rpc)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post("/api/v2/capability-analyze", headers=headers(r), json=r["request"])
            assert response.status_code == 503
    assert not queue.calls and await store.active_count() == 0
    await runtime.redis.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["acquisition", "first_service", "after_acquisition"])
async def test_submit_actual_worker_and_poll_completed_reference(monkeypatch, mode):
    import json
    from app.jobs_v22.capability_executor import CapabilityV22Executor
    from app.jobs_v22.capability_result_persistence import SupabaseCapabilityResultPersister
    from app.jobs_v22.digest import verified_request_digest
    from app.jobs_v22.worker import execute_v22_job
    app, runtime, store, queue = build_app(monkeypatch)
    m, _, _, task, resolved = graph(mode)
    r = receipt()
    r["request"]["capability_request"] = task.model_dump(mode="json")
    r.update(user_id=str(m.user_id), job_id=str(m.job_id), case_id=str(m.case_id))
    completed = None
    calls = []
    def respond(sent):
        nonlocal completed
        name = sent.url.path.rsplit("/", 1)[-1]
        calls.append(name)
        if name == "get_v22_capability_dispatch":
            return httpx.Response(200,json=r)
        if name == "resolve_v22_capability_analysis_input":
            return httpx.Response(200,json=resolved)
        assert name == "complete_v22_capability_analysis"
        completed = json.loads(sent.content)["p_payload"]
        return httpx.Response(200,json=[dict(report_id=str(m.job_id),payload_checksum=verified_request_digest(completed),idempotent=False)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as rpc:
        configure(monkeypatch, runtime, rpc)
        executor = CapabilityV22Executor(
            resolver=runtime.capability_repository,
            persister=SupabaseCapabilityResultPersister(url="https://storage.example",service_role_key="test-key",http_client=rpc))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://test") as client:
            submitted = await client.post("/api/v2/capability-analyze",headers=headers(r),json=r["request"])
            assert submitted.status_code == 202
            assert queue.calls == [(m.job_id,1)]
            await execute_v22_job(dict(redis=runtime.redis,store=store,executor=executor),str(m.job_id),1)
            polled = await client.get(f"/api/v2/tasks/{m.job_id}",headers=headers(r))
            assert polled.status_code == 200
            assert polled.json()["status"] == "succeeded"
            assert polled.json()["report_reference"]["report_id"] == str(m.job_id)
            assert polled.json()["report_reference"]["report_type"] == task.report_type
            assert "report" not in polled.json() and "input_manifest" not in polled.text
            assert completed is not None
            assert calls == ["get_v22_capability_dispatch","resolve_v22_capability_analysis_input","complete_v22_capability_analysis"]
    await runtime.redis.aclose()
