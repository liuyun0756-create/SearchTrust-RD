import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import fakeredis.aioredis
import httpx
import pytest
from arq.worker import Retry
from pydantic import SecretStr

from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.executor import ProspectV22Executor, UnavailableV22Executor
from app.jobs_v22.verified_executor import VerifiedV22Executor, V22ExecutorRouter
from app.jobs_v22.verified_input_resolver import SupabaseVerifiedInputResolver
from app.jobs_v22.verified_report_pipeline import VerifiedReportPipeline
from app.jobs_v22.verified_result_persistence import SupabaseVerifiedResultPersister
from app.jobs_v22.models import JobState
from app.jobs_v22.store import DurableJobStore
from app.jobs_v22.worker import execute_v22_job, on_shutdown, on_startup
from app.jobs_v22.verified_reconciler import reconcile_v22_verified_orphans
from app.core.config import settings
from app.competitors_v22.selection import AnalysisDiscoveryLink, AnalysisRequestEnvelope
from app.api.v2.models import AnalyzeRequest
from app.report_v22.models import ReportV22
from test_api_v2_jobs import prospect_analyze_payload, verified_task_payload
from verified_pipeline_helpers import frozen_public_fixture, resolve_fixture


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
CASE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime.now(timezone.utc)
CONTRACT_DIR = Path(__file__).resolve().parents[1] / "contracts" / "v2.2"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class RecordingExecutor:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.last_request = None

    async def execute(self, *, job_id, request, submitted_at, checkpoints, cost_ledger=None):
        self.last_request = request
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RecordingCostOutbox:
    def __init__(self) -> None:
        self.summaries = []
        self.synced = []

    async def enqueue(self, summary) -> None:
        self.summaries.append(summary)

    async def sync(self, job_id) -> bool:
        self.synced.append(job_id)
        return True


async def build_context(executor: object, *, max_attempts: int = 3):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    store = DurableJobStore(redis, prefix="test:v22", state_ttl_seconds=604800)
    await store.register_job(
        job_id=JOB_ID,
        case_id=CASE_ID,
        idempotency_key="intent-1",
        request_payload={"case_id": str(CASE_ID), "report_type": "prospect"},
        now=NOW,
    )
    return {
        "store": store,
        "executor": executor,
        "max_attempts": max_attempts,
        "state_ttl_seconds": 604800,
        "redis": redis,
        "job_try": 1,
    }, store


def prospect_report() -> ReportV22:
    payload = (CONTRACT_DIR / "fixtures" / "prospect.json").read_text(encoding="utf-8")
    return ReportV22.model_validate_json(payload)


def verified_report() -> ReportV22:
    payload = (CONTRACT_DIR / "fixtures" / "verified.json").read_text(encoding="utf-8")
    return ReportV22.model_validate_json(payload)


def verified_envelope() -> dict:
    return {
        "schema_version": "v22_verified_request_envelope_v1",
        "verified_request": verified_task_payload(),
    }


@pytest.mark.anyio
async def test_worker_success_updates_attempt_and_terminal_state() -> None:
    executor = RecordingExecutor([prospect_report()])
    ctx, store = await build_context(executor)

    await execute_v22_job(ctx, str(JOB_ID), 1)
    state = await store.require_state(JOB_ID)

    assert state.status == "succeeded"
    assert state.attempt_count == 1
    assert state.report is not None
    assert state.cost_counters["job_attempts"] == 1
    assert state.cost_counters["provider_attempts_total"] == 0
    assert executor.calls == 1


@pytest.mark.anyio
async def test_transient_failure_retries_then_becomes_explicit_terminal_failure() -> None:
    executor = RecordingExecutor(
        [TransientJobError("PROVIDER_UNAVAILABLE", "The provider is temporarily unavailable.")]
    )
    ctx, store = await build_context(executor)

    with pytest.raises(Retry):
        await execute_v22_job(ctx, str(JOB_ID), 1)
    with pytest.raises(Retry):
        await execute_v22_job(ctx, str(JOB_ID), 1)
    await execute_v22_job(ctx, str(JOB_ID), 1)

    state = await store.require_state(JOB_ID)
    assert state.status == "failed"
    assert state.attempt_count == 3
    assert state.cost_counters["job_attempts"] == 3
    assert state.error is not None
    assert state.error.error_code == "JOB_RETRY_EXHAUSTED"
    assert state.error.retryable is True


@pytest.mark.anyio
async def test_deterministic_failure_does_not_retry() -> None:
    executor = RecordingExecutor(
        [DeterministicJobError("V22_INPUT_INVALID", "The request is invalid.")]
    )
    ctx, store = await build_context(executor)

    await execute_v22_job(ctx, str(JOB_ID), 1)

    state = await store.require_state(JOB_ID)
    assert state.status == "failed"
    assert state.attempt_count == 1
    assert state.error is not None
    assert state.error.retryable is False


@pytest.mark.anyio
async def test_worker_cancellation_never_writes_failed_terminal_state() -> None:
    executor = RecordingExecutor([asyncio.CancelledError()])
    ctx, store = await build_context(executor)

    with pytest.raises(asyncio.CancelledError):
        await execute_v22_job(ctx, str(JOB_ID), 1)

    state = await store.require_state(JOB_ID)
    assert state.status == "running"
    assert state.error is None


@pytest.mark.anyio
async def test_cancelled_worker_run_can_be_executed_again_to_completion() -> None:
    executor = RecordingExecutor([asyncio.CancelledError(), prospect_report()])
    ctx, store = await build_context(executor)

    with pytest.raises(asyncio.CancelledError):
        await execute_v22_job(ctx, str(JOB_ID), 1)
    await execute_v22_job(ctx, str(JOB_ID), 1)

    state = await store.require_state(JOB_ID)
    assert state.status == "succeeded"
    assert state.attempt_count == 2
    assert executor.calls == 2


@pytest.mark.anyio
async def test_callback_outage_never_changes_successful_analysis_outcome() -> None:
    executor = RecordingExecutor([prospect_report()])
    ctx, store = await build_context(executor)

    class UnavailableSynchronizer:
        async def sync(self, job_id):
            raise RuntimeError("callback unavailable")

    ctx["callback_synchronizer"] = UnavailableSynchronizer()
    await execute_v22_job(ctx, str(JOB_ID), 1)

    assert (await store.require_state(JOB_ID)).status == "succeeded"


@pytest.mark.anyio
async def test_duplicate_worker_execution_does_not_create_second_terminal_result() -> None:
    executor = RecordingExecutor([prospect_report()])
    ctx, store = await build_context(executor)

    await execute_v22_job(ctx, str(JOB_ID), 1)
    first = await store.require_state(JOB_ID)
    await execute_v22_job(ctx, str(JOB_ID), 1)
    duplicate = await store.require_state(JOB_ID)

    assert duplicate == first
    assert executor.calls == 1


@pytest.mark.anyio
async def test_stale_physical_generation_is_ignored() -> None:
    executor = RecordingExecutor([prospect_report()])
    ctx, store = await build_context(executor)

    result = await execute_v22_job(ctx, str(JOB_ID), 2)

    assert result is None
    assert (await store.require_state(JOB_ID)).status == "queued"
    assert executor.calls == 0


@pytest.mark.anyio
async def test_worker_preserves_internal_discovery_envelope_for_executor() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    store = DurableJobStore(redis, prefix="test:v22", state_ttl_seconds=604800)
    analyze = AnalyzeRequest.model_validate_json(json.dumps(prospect_analyze_payload()))
    envelope = AnalysisRequestEnvelope(
        schema_version="v22_analysis_request_envelope_v1",
        analyze_request=analyze,
        competitor_discovery=AnalysisDiscoveryLink(
            discovery_id=UUID("22222222-2222-4222-8222-222222222222"),
            candidate_digest="sha256:" + "a" * 64,
            market_snapshot_id=UUID("44444444-4444-4444-8444-444444444444"),
            market_snapshot_checksum="sha256:" + "b" * 64,
        ),
    )
    await store.register_job(
        job_id=JOB_ID,
        case_id=CASE_ID,
        idempotency_key="enveloped-intent",
        request_payload=envelope.model_dump(mode="json"),
        now=NOW,
    )
    executor = RecordingExecutor([prospect_report()])
    ctx = {
        "store": store,
        "executor": executor,
        "max_attempts": 3,
        "state_ttl_seconds": 604800,
        "redis": redis,
    }

    await execute_v22_job(ctx, str(JOB_ID), 1)

    assert executor.calls == 1
    assert executor.last_request == envelope.model_dump(mode="json")


@pytest.mark.anyio
async def test_verified_success_persists_verified_report_cost_kind() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    store = DurableJobStore(redis, prefix="test:v22:verified", state_ttl_seconds=604800)
    payload = verified_envelope()
    await store.register_job(
        job_id=JOB_ID,
        case_id=CASE_ID,
        idempotency_key="verified-paid-attempt",
        request_payload=payload,
        now=NOW,
    )
    outbox = RecordingCostOutbox()
    executor = RecordingExecutor([verified_report()])
    ctx = {
        "store": store,
        "executor": executor,
        "cost_summary_outbox": outbox,
        "max_attempts": 3,
        "state_ttl_seconds": 604800,
        "redis": redis,
    }

    await execute_v22_job(ctx, str(JOB_ID), 1)

    assert executor.last_request == payload
    assert len(outbox.summaries) == 1
    assert outbox.summaries[0].job_kind == "verified_report"
    assert outbox.summaries[0].status == "succeeded"
    assert outbox.synced == [JOB_ID]


@pytest.mark.anyio
async def test_verified_automatic_retries_reuse_same_paid_attempt_and_settle_once() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    store = DurableJobStore(redis, prefix="test:v22:retry", state_ttl_seconds=604800)
    payload = verified_envelope()
    await store.register_job(
        job_id=JOB_ID,
        case_id=CASE_ID,
        idempotency_key="one-paid-verified-attempt",
        request_payload=payload,
        now=NOW,
    )
    outbox = RecordingCostOutbox()
    executor = RecordingExecutor(
        [TransientJobError("PROVIDER_UNAVAILABLE", "Temporarily unavailable")]
    )
    ctx = {
        "store": store,
        "executor": executor,
        "cost_summary_outbox": outbox,
        "max_attempts": 3,
        "state_ttl_seconds": 604800,
        "redis": redis,
    }

    with pytest.raises(Retry):
        await execute_v22_job(ctx, str(JOB_ID), 1)
    with pytest.raises(Retry):
        await execute_v22_job(ctx, str(JOB_ID), 1)
    await execute_v22_job(ctx, str(JOB_ID), 1)
    await execute_v22_job(ctx, str(JOB_ID), 1)

    state = await store.require_state(JOB_ID)
    assert state.status == "failed"
    assert state.attempt_count == 3
    assert executor.calls == 3
    assert executor.last_request == payload
    assert len(outbox.summaries) == 1
    assert outbox.summaries[0].job_id == JOB_ID
    assert outbox.summaries[0].job_kind == "verified_report"
    assert outbox.summaries[0].status == "failed"


@pytest.mark.anyio
async def test_verified_terminal_failure_callback_is_emitted_once_for_db_compensation() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    store = DurableJobStore(redis, prefix="test:v22:callback", state_ttl_seconds=604800)
    await store.register_job(
        job_id=JOB_ID,
        case_id=CASE_ID,
        idempotency_key="verified-terminal-callback",
        request_payload=verified_envelope(),
        now=NOW,
    )

    class RecordingSynchronizer:
        def __init__(self):
            self.statuses = []

        async def sync(self, job_id):
            self.statuses.append((await store.require_state(job_id)).status)
            return True

    synchronizer = RecordingSynchronizer()
    ctx = {
        "store": store,
        "executor": RecordingExecutor(
            [DeterministicJobError("V22_VERIFIED_RESULT_INVALID", "Invalid result")]
        ),
        "callback_synchronizer": synchronizer,
        "max_attempts": 3,
        "state_ttl_seconds": 604800,
        "redis": redis,
    }

    await execute_v22_job(ctx, str(JOB_ID), 1)
    await execute_v22_job(ctx, str(JOB_ID), 1)

    assert synchronizer.statuses.count("failed") == 1


@pytest.mark.anyio
async def test_verified_worker_recovers_after_atomic_persist_commit_ack_is_lost() -> None:
    frozen_payload, _, _, _ = frozen_public_fixture()
    _, verified_request = await resolve_fixture()
    envelope = {
        "schema_version": "v22_verified_request_envelope_v1",
        "verified_request": verified_request.model_dump(mode="json"),
    }
    database = {
        "status": "queued",
        "report_id": None,
        "charge_state": "reserved",
        "debit_count": 1,
        "stored_generation": 1,
        "report_payload": None,
    }
    persisted_payloads: list[dict] = []
    resolved_generations: list[int] = []
    persisted_generations: list[int] = []

    async def database_rpc(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content)
        if path.endswith("/resolve_v22_verified_analysis_input"):
            requested_generation = body["p_run_generation"]
            resolved_generations.append(requested_generation)
            active = (
                database["status"] in {"queued", "running"}
                and requested_generation == database["stored_generation"]
            )
            settled_takeover_replay = (
                database["status"] == "succeeded"
                and database["report_id"] == str(JOB_ID)
                and database["charge_state"] == "consumed"
                and database["report_payload"] is not None
                and requested_generation >= database["stored_generation"]
            )
            return httpx.Response(
                200 if active or settled_takeover_replay else 409,
                json=(frozen_payload if active or settled_takeover_replay
                      else {"code": "invalid"}),
                request=request,
            )
        if path.endswith("/persist_v22_verified_result"):
            persisted_payloads.append(body)
            persisted_generations.append(body["p_run_generation"])
            if database["status"] in {"queued", "running"}:
                assert body["p_run_generation"] == database["stored_generation"] == 1
                database.update(
                    status="succeeded",
                    report_id=str(JOB_ID),
                    charge_state="consumed",
                    report_payload=body["p_report_payload"],
                )
                # PostgreSQL committed all three rows; only the acknowledgement
                # disappeared because the worker process exited.
                raise asyncio.CancelledError
            assert body["p_run_generation"] > database["stored_generation"]
            assert body["p_report_payload"] == database["report_payload"]
            return httpx.Response(
                200,
                json=[{"report_id": str(JOB_ID), "idempotent": True}],
                request=request,
            )
        raise AssertionError(f"unexpected RPC path: {path}")

    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    store = DurableJobStore(
        redis, prefix="test:v22:verified-commit-loss", state_ttl_seconds=604800
    )
    await store.register_job(
        job_id=JOB_ID,
        case_id=CASE_ID,
        idempotency_key="verified-commit-ack-loss",
        request_payload=envelope,
        now=NOW,
    )

    class DatabaseCallback:
        async def sync(self, job_id):
            redis_state = await store.require_state(job_id)
            if database["status"] != "succeeded":
                database["status"] = redis_state.status
            return True

    outbox = RecordingCostOutbox()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(database_rpc)
    ) as storage_client:
        executor = V22ExecutorRouter(
            prospect_executor=UnavailableV22Executor(),
            verified_executor=VerifiedV22Executor(
                resolver=SupabaseVerifiedInputResolver(
                    url="https://project.supabase.co",
                    service_role_key="fake-service",
                    http_client=storage_client,
                ),
                pipeline=VerifiedReportPipeline(clock=lambda: NOW),
                persister=SupabaseVerifiedResultPersister(
                    url="https://project.supabase.co",
                    service_role_key="fake-service",
                    http_client=storage_client,
                ),
            ),
        )
        ctx = {
            "store": store,
            "executor": executor,
            "callback_synchronizer": DatabaseCallback(),
            "cost_summary_outbox": outbox,
            "max_attempts": 3,
            "state_ttl_seconds": 604800,
            "redis": redis,
        }
        with pytest.raises(asyncio.CancelledError):
            await execute_v22_job(ctx, str(JOB_ID), 1)
        abandoned = await store.require_state(JOB_ID)
        assert abandoned.status == "running"
        assert abandoned.run_generation == 1

        takeover = await store.take_over_stale(
            JOB_ID,
            expected_generation=1,
            now=NOW + timedelta(minutes=4),
        )
        assert takeover.applied is True
        assert takeover.state.status == "queued"
        assert takeover.state.run_generation == 2
        await execute_v22_job(ctx, str(JOB_ID), 2)

    state = await store.require_state(JOB_ID)
    assert state.status == "succeeded"
    assert state.run_generation == 2
    assert state.attempt_count == 2
    assert state.report is not None
    assert state.report.report_version.report_type == "verified_execution"
    assert database == {
        "status": "succeeded",
        "report_id": str(JOB_ID),
        "charge_state": "consumed",
        "debit_count": 1,
        "stored_generation": 1,
        "report_payload": persisted_payloads[0]["p_report_payload"],
    }
    assert len(persisted_payloads) == 2
    assert persisted_payloads[0]["p_report_payload"] == persisted_payloads[1]["p_report_payload"]
    assert resolved_generations == [1, 2]
    assert persisted_generations == [1, 2]
    assert len(outbox.summaries) == 1
    assert outbox.summaries[0].job_kind == "verified_report"
    assert outbox.summaries[0].status == "succeeded"
    assert outbox.summaries[0].attempt_count == 2
    assert outbox.summaries[0].cost_counters.root["job_attempts"] == 2


@pytest.mark.anyio
async def test_production_executor_is_explicitly_unavailable_and_never_imports_v1() -> None:
    executor = UnavailableV22Executor()

    with pytest.raises(DeterministicJobError, match="V22_PIPELINE_NOT_READY"):
        await executor.execute(
            job_id=JOB_ID,
            request={},
            submitted_at=NOW,
            checkpoints=None,
        )

    assert "app.tasks.pipeline" not in inspect.getsource(UnavailableV22Executor.execute)


@pytest.mark.anyio
async def test_worker_builds_real_isolated_executor_only_when_analyze_is_enabled(
    monkeypatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr(settings, "V22_ANALYZE_ENABLED", True)
    ctx = {"redis": redis}

    await on_startup(ctx)
    try:
        assert isinstance(ctx["executor"], V22ExecutorRouter)
        assert isinstance(ctx["executor"].prospect_executor, ProspectV22Executor)
        assert isinstance(ctx["executor"].verified_executor, UnavailableV22Executor)
        assert ctx["executor"].prospect_executor.customer_public_gbp_stage.__class__.__name__ == (
            "CheckpointedCustomerPublicGbpStage")
        assert "app.tasks.pipeline" not in inspect.getsource(
            ProspectV22Executor.execute
        )
    finally:
        await on_shutdown(ctx)


@pytest.mark.anyio
async def test_gbp_sync_stays_off_while_expired_content_cleanup_remains_available(
    monkeypatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr(settings, "V22_GBP_SYNC_ENABLED", False)
    monkeypatch.setattr(settings, "V22_SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setattr(settings, "V22_SUPABASE_SERVICE_ROLE_KEY", SecretStr("fake-service"))
    ctx = {"redis": redis}

    await on_startup(ctx)
    try:
        assert "gbp_provider" not in ctx
        assert "gbp_token_broker" not in ctx
        assert "gbp_sync_repository" not in ctx
        assert ctx["gbp_cleanup_repository"].source == "gbp"
    finally:
        await on_shutdown(ctx)

    assert ctx["gbp_cleanup_http_client"].is_closed


@pytest.mark.anyio
async def test_worker_wires_verified_independently_with_its_own_bounded_storage_client(
    monkeypatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr(settings, "V22_ANALYZE_ENABLED", True)
    monkeypatch.setattr(settings, "V22_VERIFIED_ANALYSIS_ENABLED", True)
    monkeypatch.setattr(settings, "V22_SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setattr(
        settings, "V22_SUPABASE_SERVICE_ROLE_KEY", SecretStr("fake-service")
    )
    monkeypatch.setattr(settings, "V22_CALLBACK_URL", "https://app.example.com/callback")
    monkeypatch.setattr(settings, "V22_CALLBACK_SECRET", SecretStr("fake-callback"))
    ctx = {"redis": redis}

    await on_startup(ctx)
    verified_client = ctx["verified_http_client"]
    reconciler_client = ctx["verified_reconciler_http_client"]
    prospect_client = ctx["result_http_client"]
    try:
        router = ctx["executor"]
        assert isinstance(router, V22ExecutorRouter)
        assert isinstance(router.prospect_executor, ProspectV22Executor)
        assert isinstance(router.verified_executor, VerifiedV22Executor)
        assert verified_client is not prospect_client
        assert router.verified_executor.resolver.rpc.client is verified_client
        assert router.verified_executor.persister.rpc.client is verified_client
        assert reconciler_client is not verified_client
        assert ctx["verified_orphan_reconciler"].rpc.client is reconciler_client
        assert "site_stage" not in vars(router.verified_executor)
        assert "copy_provider" not in vars(router.verified_executor)
    finally:
        await on_shutdown(ctx)

    assert verified_client.is_closed
    assert reconciler_client.is_closed
    assert prospect_client.is_closed


@pytest.mark.anyio
async def test_worker_can_enable_verified_while_prospect_stays_unavailable(
    monkeypatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr(settings, "V22_ANALYZE_ENABLED", False)
    monkeypatch.setattr(settings, "V22_VERIFIED_ANALYSIS_ENABLED", True)
    monkeypatch.setattr(settings, "V22_SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setattr(
        settings, "V22_SUPABASE_SERVICE_ROLE_KEY", SecretStr("fake-service")
    )
    monkeypatch.setattr(settings, "V22_CALLBACK_URL", "https://app.example.com/callback")
    monkeypatch.setattr(settings, "V22_CALLBACK_SECRET", SecretStr("fake-callback"))
    ctx = {"redis": redis}

    await on_startup(ctx)
    try:
        assert isinstance(ctx["executor"].prospect_executor, UnavailableV22Executor)
        assert isinstance(ctx["executor"].verified_executor, VerifiedV22Executor)
        assert "copy_http_client" not in ctx
        assert "result_http_client" not in ctx
    finally:
        await on_shutdown(ctx)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("callback_url", "callback_secret"),
    [
        ("", "fake-callback"),
        ("https://app.example.com/callback", ""),
        ("", ""),
    ],
)
async def test_verified_executor_fails_closed_without_complete_callback_configuration(
    monkeypatch, callback_url: str, callback_secret: str
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr(settings, "V22_ANALYZE_ENABLED", False)
    monkeypatch.setattr(settings, "V22_VERIFIED_ANALYSIS_ENABLED", True)
    monkeypatch.setattr(settings, "V22_SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setattr(
        settings, "V22_SUPABASE_SERVICE_ROLE_KEY", SecretStr("fake-service")
    )
    monkeypatch.setattr(settings, "V22_CALLBACK_URL", callback_url)
    monkeypatch.setattr(settings, "V22_CALLBACK_SECRET", SecretStr(callback_secret))
    ctx = {"redis": redis}

    await on_startup(ctx)
    try:
        assert isinstance(ctx["executor"].verified_executor, UnavailableV22Executor)
        assert "verified_http_client" not in ctx
        assert "verified_orphan_reconciler" in ctx
        assert "verified_reconciler_http_client" in ctx
    finally:
        reconciler_client = ctx["verified_reconciler_http_client"]
        await on_shutdown(ctx)

    assert reconciler_client.is_closed


@pytest.mark.anyio
async def test_verified_switch_off_still_reconciles_previously_paid_orphans(
    monkeypatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr(settings, "V22_ANALYZE_ENABLED", False)
    monkeypatch.setattr(settings, "V22_VERIFIED_ANALYSIS_ENABLED", False)
    monkeypatch.setattr(settings, "V22_SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setattr(
        settings, "V22_SUPABASE_SERVICE_ROLE_KEY", SecretStr("fake-service")
    )
    ctx = {"redis": redis}

    await on_startup(ctx)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=[])

    try:
        assert isinstance(ctx["executor"].verified_executor, UnavailableV22Executor)
        reconciler = ctx["verified_orphan_reconciler"]
        original_client = reconciler.rpc.client
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            reconciler.rpc.client = client
            await reconcile_v22_verified_orphans(ctx)
        reconciler.rpc.client = original_client
        assert len(requests) == 1
        assert requests[0].url.path == "/rest/v1/rpc/expire_v22_stale_verified_jobs"
    finally:
        reconciler_client = ctx["verified_reconciler_http_client"]
        await on_shutdown(ctx)

    assert reconciler_client.is_closed


@pytest.mark.anyio
async def test_missing_service_role_keeps_disabled_verified_reconciliation_a_safe_noop(
    monkeypatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr(settings, "V22_ANALYZE_ENABLED", False)
    monkeypatch.setattr(settings, "V22_VERIFIED_ANALYSIS_ENABLED", False)
    monkeypatch.setattr(settings, "V22_SUPABASE_URL", "")
    monkeypatch.setattr(settings, "V22_SUPABASE_SERVICE_ROLE_KEY", SecretStr(""))
    ctx = {"redis": redis}

    await on_startup(ctx)
    try:
        assert "verified_orphan_reconciler" not in ctx
        assert "verified_reconciler_http_client" not in ctx
        await reconcile_v22_verified_orphans(ctx)
    finally:
        await on_shutdown(ctx)
