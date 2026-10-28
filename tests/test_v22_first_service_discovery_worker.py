"""Real discovery worker/store/service, controlled providers and SQL transport.

SQL authority/refund assertions live in the PGlite migration suite; this harness
does not pretend that mocked RPC state proves PostgreSQL concurrency.
"""
import asyncio
from datetime import timedelta
import json
from uuid import uuid4
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest

from app.competitors_v22.discovery_service import CompetitorDiscoveryService
from app.competitors_v22.market_store import SharedMarketSnapshotStore
from app.competitors_v22.store import CompetitorDiscoveryStore
from app.jobs_v22.errors import TransientJobError, DeterministicJobError
from app.jobs_v22.first_service_discovery_repository import FirstServiceDiscoveryRepository
from app.jobs_v22.first_service_discovery_worker import (
    FirstServiceDiscoveryCoordinator, execute_v22_first_service_discovery,
    reconcile_v22_first_service_discovery,
)
from app.jobs_v22.keys import JobRedisKeys
from test_v22_competitor_models import NOW, CASE_ID, JOB_ID, discovery_request
from test_v22_competitor_discovery_service import RecordingMarketStage, market_with_three_competitors


@pytest.fixture
def anyio_backend():
    return "asyncio"


class SqlBoundary:
    def __init__(self):
        self.workflow, self.owner = uuid4(), uuid4()
        self.discovery_id = JOB_ID
        self.request = discovery_request().model_dump(mode="json")
        self.now = NOW
        self.marker = self.lease_token = self.result = None
        self.attempts = self.failures = 0
        self.closed = False
        self.fail_rpc = None
        self.lose_ack = None
        self.calls = []
        self.deadline = NOW + timedelta(minutes=20)
        self.heartbeat_ok = True

    def envelope(self, outcome):
        return dict(outcome=outcome, workflow_id=str(self.workflow), user_id=str(self.owner),
            case_id=str(CASE_ID), discovery_job_id=str(self.discovery_id), request=self.request,
            deadline_at=self.deadline.isoformat(), attempts=self.attempts,
            lease_until=(self.now + timedelta(seconds=90)).isoformat() if self.lease_token else None,
            cost_ledger_job_id=str(JOB_ID), cost_ledger_created_at=NOW.isoformat(),
            dispatch_token=self.marker)

    def respond(self, request):
        rpc = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        self.calls.append((rpc, body))
        assert request.headers["authorization"] == "Bearer service-test"
        if rpc == self.fail_rpc:
            return httpx.Response(503, json={"secret": "must not be logged"})
        value = None
        if rpc == "expire_v22_first_service_discoveries":
            value = []
        elif rpc == "list_v22_first_service_discovery_work":
            value = [] if self.closed or self.result else [dict(workflow_id=str(self.workflow))]
        elif rpc == "claim_v22_first_service_discovery_dispatch":
            fresh = self.marker is None
            if fresh:
                self.marker = body["p_dispatch_token"]
            value = self.envelope("dispatch" if fresh else "observe")
        elif rpc == "claim_v22_first_service_discovery_run":
            if self.closed or self.result or self.deadline <= self.now or body["p_discovery_job_id"] != str(self.discovery_id):
                value = dict(outcome="closed")
            elif self.lease_token:
                value = dict(outcome="busy")
            elif self.attempts >= 3:
                value = dict(outcome="exhausted")
            else:
                self.attempts += 1
                self.lease_token = body["p_lease_token"]
                value = self.envelope("claimed")
        elif rpc == "heartbeat_v22_first_service_discovery":
            value = self.heartbeat_ok and body["p_lease_token"] == self.lease_token and not self.closed
        elif rpc == "release_v22_first_service_discovery":
            value = body["p_lease_token"] == self.lease_token
            if value:
                self.lease_token = None
        elif rpc == "record_v22_first_service_discovery":
            assert body["p_workflow_id"] == str(self.workflow) and body["p_case_id"] == str(CASE_ID)
            assert body["p_result"]["discovery_id"] == str(self.discovery_id)
            self.result = body["p_result"]
            value = dict(phase="awaiting_confirmation")
        elif rpc == "fail_v22_first_service_discovery":
            assert body["p_reason"] == "DISCOVERY_FAILED"
            assert body["p_discovery_job_id"] == str(self.discovery_id)
            self.failures += not self.closed
            self.closed = True
            value = True
        else:
            raise AssertionError(rpc)
        if rpc == self.lose_ack:
            self.lose_ack = None
            raise httpx.ReadTimeout("response lost after commit")
        return httpx.Response(200, json=value)


@pytest.fixture
async def harness():
    sql = SqlBoundary()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    redis.enqueue_job = AsyncMock(return_value=None)
    store = CompetitorDiscoveryStore(redis, prefix="test:first-service-discovery", state_ttl_seconds=3600)
    market = SharedMarketSnapshotStore(redis, prefix="test:shared", ttl_seconds=86400)
    stage = RecordingMarketStage(market_with_three_competitors())
    service = CompetitorDiscoveryService(market_stage=stage, market_store=market, clock=lambda: NOW)
    async with httpx.AsyncClient(transport=httpx.MockTransport(sql.respond)) as client:
        repo = FirstServiceDiscoveryRepository(url="https://storage.example", service_role_key="service-test", http_client=client)
        coordinator = FirstServiceDiscoveryCoordinator(repository=repo, store=store, market_store=market,
            redis=redis, queue_name="test:queue")
        ctx = dict(redis=redis, first_service_discovery_coordinator=coordinator,
            competitor_discovery_service=service, clock=lambda: NOW,
            first_service_discovery_heartbeat_seconds=0.005)
        yield sql, ctx, coordinator, stage
    await redis.aclose()


async def run(h):
    sql, ctx, _, _ = h
    await execute_v22_first_service_discovery(ctx, str(sql.workflow), str(sql.discovery_id))


def next_round(h, *, refresh=False, supplements=None):
    """Model only the receipt transport; SQL continuation is tested in PGlite."""
    sql, ctx, _, _ = h
    sql.discovery_id = uuid4()
    sql.marker = sql.result = sql.lease_token = None
    if supplements is not None:
        sql.request["supplemental_website_urls"] = supplements
    if refresh:
        sql.now = NOW + timedelta(hours=25)
    sql.deadline = sql.now + timedelta(minutes=20)
    ctx["clock"] = lambda: sql.now
    ctx["competitor_discovery_service"].clock = lambda: sql.now


@pytest.mark.anyio
async def test_supplement_then_expiry_refresh_share_one_budget_and_one_cost_summary_identity(harness):
    sql, ctx, coordinator, stage = harness
    outbox = AsyncMock()
    ctx["cost_summary_outbox"] = outbox
    service = ctx["competitor_discovery_service"]
    service.supplemental_validator = AsyncMock()
    service.supplemental_validator.validate.return_value = True
    await reconcile_v22_first_service_discovery(ctx)
    await run(harness)
    first_market = sql.result["market_snapshot_id"]
    next_round(harness, supplements=["https://rival-1.example/"])
    await reconcile_v22_first_service_discovery(ctx)
    await execute_v22_first_service_discovery(ctx, str(sql.workflow), str(JOB_ID))
    assert sql.attempts == 1  # Stale physical queue message is rejected.
    await run(harness)
    assert sql.attempts == 2 and len(stage.calls) == 1
    assert sql.result["market_snapshot_id"] == first_market
    service.supplemental_validator.validate.assert_awaited_once()
    second_id = sql.discovery_id
    next_round(harness, refresh=True)
    await reconcile_v22_first_service_discovery(ctx)
    await run(harness)
    assert sql.attempts == 3 and len(stage.calls) == 2 and sql.failures == 0
    assert sql.result["market_snapshot_id"] != first_market
    summaries = [call.args[0] for call in outbox.enqueue.call_args_list]
    assert len(summaries) == 3
    assert {s.job_id for s in summaries} == {JOB_ID}
    assert [s.attempt_count for s in summaries] == [1, 2, 3]
    assert summaries[0].ledger_revision < summaries[1].ledger_revision < summaries[2].ledger_revision
    assert {s.started_at for s in summaries} == {NOW}
    keys = JobRedisKeys(coordinator.store.keys.prefix)
    assert await ctx["redis"].exists(keys.cost_ledger(JOB_ID))
    assert not await ctx["redis"].exists(keys.cost_ledger(second_id), keys.cost_ledger(sql.discovery_id))


@pytest.mark.anyio
async def test_new_round_cannot_replace_a_lost_original_cost_ledger(harness):
    sql, ctx, coordinator, stage = harness
    await reconcile_v22_first_service_discovery(ctx)
    await run(harness)
    await ctx["redis"].delete(JobRedisKeys(coordinator.store.keys.prefix).cost_ledger(JOB_ID))
    next_round(harness, refresh=True)
    await reconcile_v22_first_service_discovery(ctx)
    await run(harness)
    assert len(stage.calls) == 1 and sql.failures == 0 and sql.result is None
    assert not await ctx["redis"].exists(JobRedisKeys(coordinator.store.keys.prefix).cost_ledger(sql.discovery_id))


@pytest.mark.anyio
async def test_refresh_cannot_reset_exhausted_supplier_budget(harness):
    from app.jobs_v22.cost_ledger import JobCostLedger
    from app.jobs_v22.cost_models import pricing_catalog_from_settings
    from app.core.config import settings
    sql, ctx, coordinator, stage = harness
    await reconcile_v22_first_service_discovery(ctx)
    await run(harness)
    ledger = JobCostLedger(ctx["redis"], prefix=coordinator.store.keys.prefix, job_id=JOB_ID,
        ttl_seconds=3600, pricing=pricing_catalog_from_settings(settings), job_created_at=NOW, require_existing=True)
    for _ in range(30):
        await ledger.claim("serpapi_market_search")
    actual = stage.collect_context
    async def limited(**kwargs):
        assert kwargs["cost_ledger"].job_id == JOB_ID
        await kwargs["cost_ledger"].claim("serpapi_market_search")
        return await actual(**kwargs)
    stage.collect_context = limited
    next_round(harness, refresh=True)
    await reconcile_v22_first_service_discovery(ctx)
    await run(harness)
    assert len(stage.calls) == 1 and sql.failures == 1
    state = await coordinator.store.require_state(sql.discovery_id)
    assert state.error.error_code == "V22_PROVIDER_ATTEMPT_LIMIT"


@pytest.mark.anyio
async def test_last_global_attempt_does_not_get_three_more_round_local_retries(harness):
    sql, ctx, coordinator, stage = harness
    await reconcile_v22_first_service_discovery(ctx)
    await run(harness)
    next_round(harness, supplements=["https://rival-1.example/"])
    await reconcile_v22_first_service_discovery(ctx)
    await run(harness)
    next_round(harness, refresh=True)
    stage.collect_context = AsyncMock(side_effect=TransientJobError("SERP_PROVIDER_UNAVAILABLE", "retry"))
    await reconcile_v22_first_service_discovery(ctx)
    await run(harness)
    assert sql.attempts == 3 and sql.failures == 1
    assert (await coordinator.store.require_state(sql.discovery_id)).status == "failed"
    stage.collect_context.assert_awaited_once()


@pytest.mark.anyio
async def test_real_discovery_is_dispatched_generated_and_recorded_once(harness):
    sql, ctx, coordinator, stage = harness
    await reconcile_v22_first_service_discovery(ctx)
    queued = ctx["redis"].enqueue_job.call_args
    assert queued.args == ("execute_v22_first_service_discovery", str(sql.workflow), str(JOB_ID))
    assert queued.kwargs["_job_id"].endswith(":attempt:1")
    await run(harness)
    assert sql.result["ready_for_confirmation"] is True
    assert sql.result["discovery_id"] == str(JOB_ID)
    assert len(stage.calls) == 1
    assert (await coordinator.store.require_state(JOB_ID)).status == "succeeded"
    await run(harness)
    await reconcile_v22_first_service_discovery(ctx)
    assert len(stage.calls) == 1 and sql.failures == 0
    assert not any("debit" in rpc or "reserve" in rpc for rpc, _ in sql.calls)


@pytest.mark.anyio
@pytest.mark.parametrize("loss", ["queue_ack", "dispatch_ack", "redis_state", "result_ack"])
async def test_uncertain_handoffs_never_recreate_registered_state_or_repeat_sources(harness, loss):
    sql, ctx, coordinator, stage = harness
    if loss == "dispatch_ack":
        sql.lose_ack = "claim_v22_first_service_discovery_dispatch"
    if loss == "queue_ack":
        ctx["redis"].enqueue_job.side_effect = TimeoutError()
    await reconcile_v22_first_service_discovery(ctx)
    if loss == "redis_state":
        await ctx["redis"].delete(coordinator.store.keys.state(JOB_ID))
    ctx["redis"].enqueue_job.side_effect = None
    if loss == "result_ack":
        sql.lose_ack = "record_v22_first_service_discovery"
        await run(harness)
    await reconcile_v22_first_service_discovery(ctx)
    await run(harness)
    assert len(stage.calls) == (0 if loss in ("dispatch_ack", "redis_state") else 1)
    assert sql.failures == 0
    if loss in ("dispatch_ack", "redis_state"):
        assert await coordinator.store.get_state(JOB_ID) is None


@pytest.mark.anyio
async def test_transient_retry_preserves_budget_and_missing_ledger_blocks_recollection(harness):
    sql, ctx, coordinator, stage = harness
    stage.collect_context = AsyncMock(side_effect=TransientJobError("SERP_PROVIDER_UNAVAILABLE", "retry"))
    await reconcile_v22_first_service_discovery(ctx)
    await run(harness)
    assert (await coordinator.store.require_state(JOB_ID)).status == "queued"
    key = JobRedisKeys(coordinator.store.keys.prefix).cost_ledger(JOB_ID)
    assert await ctx["redis"].exists(key)
    await reconcile_v22_first_service_discovery(ctx)
    assert ctx["redis"].enqueue_job.call_args.kwargs["_job_id"].endswith(":attempt:2")
    await ctx["redis"].delete(key)
    await run(harness)
    stage.collect_context.assert_awaited_once()
    assert not await ctx["redis"].exists(key)
    assert sql.failures == 0


@pytest.mark.anyio
async def test_definitive_failure_is_settled_but_unknown_transport_is_not(harness):
    sql, ctx, _, stage = harness
    stage.collect_context = AsyncMock(side_effect=DeterministicJobError("INVALID_MARKET", "invalid"))
    await reconcile_v22_first_service_discovery(ctx)
    sql.fail_rpc = "claim_v22_first_service_discovery_run"
    await run(harness)
    assert sql.failures == 0
    stage.collect_context.assert_not_awaited()
    sql.fail_rpc = None
    await run(harness)
    assert sql.failures == 1
    await reconcile_v22_first_service_discovery(ctx)
    assert sql.failures == 1


@pytest.mark.anyio
@pytest.mark.parametrize("loss", ["lease", "heartbeat_transport", "deadline", "cancel"])
async def test_running_work_stops_when_its_authority_ends(harness, loss):
    sql, ctx, coordinator, stage = harness
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def blocked(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    stage.collect_context = blocked
    if loss == "deadline":
        sql.deadline = NOW + timedelta(seconds=0.03)
    await reconcile_v22_first_service_discovery(ctx)
    task = asyncio.create_task(run(harness))
    await asyncio.wait_for(started.wait(), 1)
    if loss == "lease":
        sql.heartbeat_ok = False
    elif loss == "heartbeat_transport":
        sql.fail_rpc = "heartbeat_v22_first_service_discovery"
    elif loss == "cancel":
        task.cancel()
    if loss == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await asyncio.wait_for(task, 1)
    assert stopped.is_set() and sql.lease_token is None
    assert sql.result is None and sql.failures == 0
    assert (await coordinator.store.require_state(JOB_ID)).status == "running"


@pytest.mark.anyio
async def test_request_substitution_never_starts_sources(harness):
    sql, ctx, coordinator, stage = harness
    await reconcile_v22_first_service_discovery(ctx)
    key = coordinator.store.keys.request(JOB_ID)
    original = await ctx["redis"].get(key)
    await ctx["redis"].set(key, b'{}')
    await run(harness)
    assert stage.calls == [] and sql.failures == 0
    await ctx["redis"].set(key, original)
    # A prior execution lease without a cost ledger is intentionally not reset.
    assert sql.attempts == 1


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["input_digest", "market_checksum", "market_missing", "result_expired"])
async def test_saved_result_requires_matching_live_evidence_before_sql_persistence(harness, change):
    sql, ctx, coordinator, stage = harness
    await reconcile_v22_first_service_discovery(ctx)
    sql.fail_rpc = "record_v22_first_service_discovery"
    await run(harness)
    assert sql.result is None and len(stage.calls) == 1
    state = await coordinator.store.require_state(JOB_ID)
    raw = state.model_dump(mode="json")
    if change == "input_digest": raw["result"]["input_digest"] = "sha256:" + "f" * 64
    elif change == "market_checksum": raw["result"]["market_snapshot_checksum"] = "sha256:" + "f" * 64
    elif change == "market_missing":
        await ctx["redis"].delete(coordinator.market_store.key(state.result.input_digest))
    else:
        ctx["clock"] = lambda: NOW + timedelta(days=2)
        sql.deadline = NOW + timedelta(days=3)  # Boundary test, not a real SQL deadline extension.
    await ctx["redis"].set(coordinator.store.keys.state(JOB_ID), json.dumps(raw))
    sql.fail_rpc = None
    await reconcile_v22_first_service_discovery(ctx)
    assert sql.result is None and sql.failures == 0 and len(stage.calls) == 1


@pytest.mark.anyio
async def test_no_eligible_candidates_is_a_completed_discovery_not_a_technical_refund(harness):
    from test_v22_competitor_candidates import snapshot
    sql, ctx, _, stage = harness
    stage.market_snapshot = snapshot([])
    await reconcile_v22_first_service_discovery(ctx)
    await run(harness)
    assert sql.result is not None and not sql.result["ready_for_confirmation"]
    assert sql.result["candidates"] == [] and sql.failures == 0


@pytest.mark.anyio
async def test_transient_attempts_are_bounded_and_keep_one_ledger(harness):
    sql, ctx, coordinator, stage = harness
    stage.collect_context = AsyncMock(side_effect=TransientJobError("SERP_PROVIDER_UNAVAILABLE", "retry"))
    for _ in range(4):
        await reconcile_v22_first_service_discovery(ctx)
        await run(harness)
    assert stage.collect_context.await_count == 3 and sql.attempts == 3
    assert sql.failures == 1
    assert (await coordinator.store.require_state(JOB_ID)).attempt_count == 3


@pytest.mark.anyio
async def test_actual_recovery_reuses_market_checkpoint_without_repeating_provider(harness):
    sql, ctx, coordinator, stage = harness
    await reconcile_v22_first_service_discovery(ctx)
    service = ctx["competitor_discovery_service"]
    actual = service.discover
    async def interrupted(**kwargs):
        await actual(**kwargs)
        raise asyncio.CancelledError()
    service.discover = interrupted
    with pytest.raises(asyncio.CancelledError):
        await run(harness)
    assert len(stage.calls) == 1 and (await coordinator.store.require_state(JOB_ID)).status == "running"
    service.discover = actual
    await reconcile_v22_first_service_discovery(ctx)
    await run(harness)
    assert sql.result is not None and len(stage.calls) == 1 and sql.attempts == 2


@pytest.mark.anyio
async def test_busy_closed_and_unconfigured_worker_cannot_start_sources(harness):
    sql, ctx, _, stage = harness
    await reconcile_v22_first_service_discovery(ctx)
    sql.lease_token = str(uuid4())
    await run(harness)
    sql.closed = True
    await run(harness)
    await execute_v22_first_service_discovery({}, str(sql.workflow), str(JOB_ID))
    await reconcile_v22_first_service_discovery({})
    assert stage.calls == [] and sql.failures == 0


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["case", "outcome", "supplement", "attempts", "unknown"])
async def test_repository_rejects_malformed_or_wrong_identity_receipts(harness, change):
    sql, _, coordinator, _ = harness
    token = uuid4()
    sql.marker = str(token)
    raw = sql.envelope("dispatch")
    if change == "case": raw["case_id"] = str(uuid4())
    if change == "outcome": raw["outcome"] = "claimed"
    if change == "supplement": raw["request"]["supplemental_website_urls"] = ["https://rival.example"]
    if change == "attempts": raw["attempts"] = True
    if change == "unknown": raw["raw_provider_payload"] = "not allowed"
    coordinator.repository.rpc.post = AsyncMock(return_value=raw)
    with pytest.raises(DeterministicJobError):
        await coordinator.repository.claim(sql.workflow, token)
