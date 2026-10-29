import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis.aioredis
import httpx
import pytest
from arq import Retry

from app.jobs_v22 import capability_acquisition_worker as worker
from app.jobs_v22.capability_acquisition_preparation import CapabilityAcquisitionPreparer
from app.jobs_v22.capability_acquisition_input import build_acquisition_input
from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository
from app.jobs_v22.cost_ledger import JobCostLedger, CostLedgerError
from app.jobs_v22.cost_models import PricingCatalog, empty_request_prices
from app.jobs_v22.queue import ArqAcquisitionPreparationQueue, physical_job_id
from app.jobs_v22.store import DurableJobStore
from test_v22_capability_acquisition_preparation import inputs, dispatch_for


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Harness:
    def __init__(self, monkeypatch):
        self.args, self.envelope, self.receipt = inputs()
        self.manifest, self.value = build_acquisition_input(**self.args)
        self.dispatch = dispatch_for(self.args, self.manifest)
        self.calls = []
        self.frozen = False
        self.attempt = 1
        self.outcome = None
        self.freeze_lost = False
        self.pulse_ok = True
        self.stage_ok = True
        self.redis = fakeredis.aioredis.FakeRedis()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        self.repo = SupabaseCapabilityInputRepository(url="https://storage.example",service_role_key="test-only",http_client=self.client)
        self.collector = SimpleNamespace(collect_sources=AsyncMock(return_value=self.args["collected"]))
        self.preparer = CapabilityAcquisitionPreparer(repository=self.repo,collector=self.collector,
            discovery_verifier=SimpleNamespace(verify=AsyncMock(return_value=self.envelope.competitor_discovery)),clock=lambda:self.args["now"])
        self.submit = AsyncMock(return_value="queued")
        self.ctx = dict(redis=self.redis,store=DurableJobStore(self.redis,prefix="test:v22",state_ttl_seconds=3600),
            state_ttl_seconds=3600,cost_pricing=PricingCatalog(revision=1,request_usd_micros=empty_request_prices()),
            max_attempts=3,heartbeat_seconds=0.01,capability_acquisition_preparer=self.preparer,
            capability_submit_dispatch=self.submit)
        monkeypatch.setattr(worker,"utc_now",lambda:self.args["now"])

    def respond(self, sent):
        name=sent.url.path.rsplit("/",1)[-1]; body=json.loads(sent.content)
        self.calls.append((name,body))
        if name=="claim_v22_acquisition_preparation":
            if self.outcome: return httpx.Response(200,json={"outcome":self.outcome})
            receipt=deepcopy(self.receipt)
            if self.frozen: receipt["dispatch"]=self.dispatch
            return httpx.Response(200,json=dict(outcome="ready" if self.frozen else "claimed",
                attempt_count=self.attempt,preparation=receipt,request=self.envelope.model_dump(mode="json")))
        if name=="begin_v22_acquisition_preparation":
            return httpx.Response(200,json=self.receipt)
        if name=="heartbeat_v22_acquisition_preparation":
            return httpx.Response(200,json=self.pulse_ok and not self.frozen)
        if name=="advance_v22_acquisition_stage":
            return httpx.Response(200,json=self.stage_ok and self.pulse_ok and not self.frozen)
        if name=="freeze_v22_acquisition_preparation":
            assert body["p_input"]["p_manifest"]==self.manifest.model_dump(mode="json")
            assert body["p_cost_counters"]["cost_ledger_revision"]>0
            self.frozen=True
            self.dispatch.update(attempt_count=self.attempt,revision=5,cost_counters=body["p_cost_counters"])
            if self.freeze_lost: raise httpx.ReadTimeout("private transport diagnostic")
            return httpx.Response(200,json=dict(manifest_id=str(self.manifest.id),input_checksum=self.manifest.input_checksum,
                generation_input_checksum=self.manifest.generation_input_checksum))
        if name=="get_v22_capability_dispatch": return httpx.Response(200,json=self.dispatch)
        raise AssertionError(name)

    async def run(self):
        return await worker.prepare_v22_acquisition(self.ctx,str(self.args["job_id"]),1)

    async def close(self):
        await self.client.aclose(); await self.redis.aclose()


@pytest.mark.anyio
async def test_real_preparer_freezes_under_lease_and_hands_same_task_to_formal_runtime(monkeypatch):
    from app.api.v2 import runtime as runtime_module
    h=Harness(monkeypatch)
    queue=SimpleNamespace(enqueue=AsyncMock(return_value=True))
    runtime=runtime_module.V22JobRuntime(store=h.ctx["store"],queue=queue,redis=h.redis,capability_repository=h.repo)
    monkeypatch.setattr(runtime_module,"datetime",SimpleNamespace(now=lambda _:h.args["now"]))
    h.ctx["capability_submit_dispatch"]=runtime.submit_capability
    try:
        result=await h.run()
        assert result.job_id==h.args["job_id"]
        state=await h.ctx["store"].require_state(h.args["job_id"])
        assert state.created_at==h.args["created_at"] and state.deadline_at==h.args["deadline_at"]
        assert state.attempt_count==1 and state.revision==5
        assert await h.ctx["store"].get_request(state.job_id)==h.dispatch["request"]
        queue.enqueue.assert_awaited_once_with(state.job_id,1)
        assert h.collector.collect_sources.await_count==1
        assert [name for name,_ in h.calls].count("freeze_v22_acquisition_preparation")==1
        assert not any("credit" in name or "complete" in name for name,_ in h.calls)
        await h.run()  # Ready replay repairs formal enqueue with no collection.
        assert h.collector.collect_sources.await_count==1 and queue.enqueue.await_count==2
    finally: await h.close()


@pytest.mark.anyio
@pytest.mark.parametrize("loss",["freeze","handoff"])
async def test_lost_acknowledgement_resumes_frozen_dispatch_without_recollection(monkeypatch,loss):
    h=Harness(monkeypatch)
    h.freeze_lost=loss=="freeze"
    if loss=="handoff": h.submit.side_effect=RuntimeError("uncertain enqueue")
    try:
        with pytest.raises(Retry): await h.run()
        assert h.frozen
        h.submit.side_effect=None
        await h.run()
        assert h.collector.collect_sources.await_count==1
        assert sum(name=="freeze_v22_acquisition_preparation" for name,_ in h.calls)==1
        assert h.submit.call_args.kwargs["job_id"]==h.args["job_id"]
    finally: await h.close()


@pytest.mark.anyio
@pytest.mark.parametrize("attempt",[2,3])
async def test_lost_budget_never_reinitializes_or_collects_after_sql_attempt(monkeypatch,attempt):
    h=Harness(monkeypatch); h.attempt=attempt
    try:
        with pytest.raises(Retry): await h.run()
        h.collector.collect_sources.assert_not_awaited()
        assert not await h.redis.exists(h.ctx["store"].keys.cost_ledger(h.args["job_id"]))
        h.submit.assert_not_awaited()
    finally: await h.close()


@pytest.mark.anyio
@pytest.mark.parametrize("reason",["lease","deadline"])
async def test_lease_loss_or_original_deadline_cancels_collection_without_handoff(monkeypatch,reason):
    h=Harness(monkeypatch); cancelled=asyncio.Event()
    async def collect(**kwargs):
        h.pulse_ok=False
        try: await asyncio.Event().wait()
        finally: cancelled.set()
    h.collector.collect_sources.side_effect=collect
    if reason=="deadline":
        monkeypatch.setattr(worker,"utc_now",lambda:h.args["deadline_at"])
    try:
        with pytest.raises(Retry): await h.run()
        if reason=="lease": assert cancelled.is_set()
        else: h.collector.collect_sources.assert_not_awaited()
        assert not h.frozen
        h.submit.assert_not_awaited()
    finally: await h.close()


@pytest.mark.anyio
@pytest.mark.parametrize("outcome",["busy","inactive"])
async def test_busy_or_stale_queue_messages_cannot_collect(monkeypatch,outcome):
    h=Harness(monkeypatch); h.outcome=outcome
    try:
        if outcome=="busy":
            with pytest.raises(Retry): await h.run()
        else: await h.run()
        h.collector.collect_sources.assert_not_awaited(); h.submit.assert_not_awaited()
    finally: await h.close()


@pytest.mark.anyio
async def test_disabled_worker_does_not_fall_through_to_legacy():
    with pytest.raises(Retry): await worker.prepare_v22_acquisition({},str(uuid4()),1)


@pytest.mark.anyio
async def test_preparation_has_distinct_physical_identity_and_bounded_queue_args():
    pool=SimpleNamespace(enqueue_job=AsyncMock(return_value=object()))
    queue=ArqAcquisitionPreparationQueue(pool,queue_name="test-queue")
    job=uuid4(); await queue.enqueue(job,2)
    call=pool.enqueue_job.call_args
    assert call.args==("prepare_v22_acquisition",str(job),2)
    assert call.kwargs["_job_id"]==physical_job_id(job,2)+":prepare"


@pytest.mark.anyio
async def test_registration_replay_repairs_enqueue_failure_without_credit_or_formal_payload_mutation(monkeypatch):
    h=Harness(monkeypatch); queue=SimpleNamespace(enqueue=AsyncMock(side_effect=RuntimeError("lost")))
    identity=dict(user_id=h.args["user_id"],job_id=h.args["job_id"],idempotency_key=h.receipt["idempotency_key"],
        business_version_id=h.args["business"].id,request=h.envelope.model_dump(mode="json"))
    try:
        with pytest.raises(RuntimeError):
            await worker.enqueue_acquisition_preparation(preparer=h.preparer,queue=queue,submit_dispatch=h.submit,**identity)
        queue.enqueue.side_effect=None
        await worker.enqueue_acquisition_preparation(preparer=h.preparer,queue=queue,submit_dispatch=h.submit,**identity)
        assert queue.enqueue.await_count==2
        assert len(set(name for name,_ in h.calls))==1
        assert await h.ctx["store"].get_request(h.args["job_id"]) is None
    finally: await h.close()


@pytest.mark.anyio
async def test_strict_cost_ledger_stops_if_deleted_during_execution(monkeypatch):
    h=Harness(monkeypatch)
    ledger=JobCostLedger(h.redis,prefix="test:v22",job_id=h.args["job_id"],ttl_seconds=3600,
        pricing=h.ctx["cost_pricing"],job_created_at=h.args["created_at"])
    try:
        await ledger.ensure(); ledger.require_existing=True
        await ledger.claim("serpapi_market_search")
        await h.redis.delete(ledger.key)
        for operation in (ledger.ensure,ledger.snapshot,lambda:ledger.claim("serpapi_market_search")):
            with pytest.raises(CostLedgerError): await operation()
        assert not await h.redis.exists(ledger.key)
    finally: await h.close()


@pytest.mark.anyio
async def test_formal_worker_cannot_recreate_budget_after_handoff(monkeypatch):
    from app.jobs_v22.worker import execute_v22_job
    h=Harness(monkeypatch)
    try:
        await h.ctx["store"].register_job(job_id=h.args["job_id"],case_id=h.manifest.case_id,
            idempotency_key=h.receipt["idempotency_key"],request_payload=h.dispatch["request"],
            now=h.args["now"],initial_attempt_count=1)
        executor=SimpleNamespace(execute=AsyncMock())
        h.ctx["executor"]=executor
        with pytest.raises(Retry): await execute_v22_job(h.ctx,str(h.args["job_id"]),1)
        executor.execute.assert_not_awaited()
        assert not await h.redis.exists(h.ctx["store"].keys.cost_ledger(h.args["job_id"]))
    finally: await h.close()


@pytest.mark.anyio
@pytest.mark.parametrize("allowed", [True, False])
async def test_collector_milestones_use_current_sql_lease_and_cannot_continue_after_rejection(monkeypatch, allowed):
    h = Harness(monkeypatch); h.stage_ok = allowed
    async def collect(**kwargs):
        for stage in ["collecting_market", "collecting_site", "collecting_competitors", "building_evidence"]:
            await kwargs["checkpoints"].report_progress(stage)
        return h.args["collected"]
    h.collector.collect_sources.side_effect = collect
    try:
        if allowed:
            await h.run()
            calls = [body for name, body in h.calls if name == "advance_v22_acquisition_stage"]
            assert [body["p_stage"] for body in calls] == ["collecting_market", "collecting_site", "collecting_competitors", "building_evidence"]
            assert all(body["p_job_id"] == str(h.args["job_id"]) and body["p_run_generation"] == 1 for body in calls)
            assert len({body["p_lease_token"] for body in calls}) == 1
        else:
            with pytest.raises(Retry): await h.run()
            assert not h.frozen
            h.submit.assert_not_awaited()
    finally:
        await h.close()
