"""ARQ worker entrypoint for isolated SearchTrust v2.2 durable tasks."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import timedelta
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from arq import Retry
from arq.connections import RedisSettings
from arq.cron import cron
from arq.worker import func
import httpx
from pydantic import SecretStr

from app.core.config import settings
from app.google_connections_v22.gsc import GscProvider
from app.google_connections_v22.ga4 import Ga4Provider
from app.google_connections_v22.gbp import GbpProvider
from app.google_connections_v22.ga4_sync_worker import execute_v22_ga4_sync, reconcile_v22_ga4_syncs
from app.google_connections_v22.gbp_sync_worker import (
    cleanup_v22_gbp_content,
    execute_v22_gbp_sync,
    reconcile_v22_gbp_syncs,
)
from app.google_connections_v22.sync_io import SyncRepository, TokenBroker
from app.google_connections_v22.sync_worker import execute_v22_gsc_sync, reconcile_v22_gsc_syncs
from app.competitors_v22.discovery_service import CompetitorDiscoveryService
from app.competitors_v22.collection_stage import CheckpointedCompetitorCollectionStage
from app.competitors_v22.market_store import SharedMarketSnapshotStore
from app.competitors_v22.public_profile_stage import build_public_profile_stage
from app.competitors_v22.reconciler import reconcile_v22_competitor_discoveries
from app.competitors_v22.site_stage import CheckpointedCompetitorSiteStage
from app.competitors_v22.store import CompetitorDiscoveryStore
from app.competitors_v22.worker import execute_v22_competitor_discovery
from app.competitors_v22.selection import AnalysisRequestEnvelope
from app.competitors_v22.supplements import SupplementalHomepageValidator
from app.jobs_v22.callbacks import CallbackSynchronizer, SignedCallbackClient
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.circuit_breaker import RedisCircuitBreaker
from app.jobs_v22.copy_provider import DifyControlledCopyProvider
from app.jobs_v22.cost_ledger import JobCostLedger
from app.jobs_v22.cost_models import CostSummaryRecord, pricing_catalog_from_settings
from app.jobs_v22.cost_persistence import CostSummaryOutbox, CostSummaryPersister
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import (
    DeterministicJobError,
    JobDeadlineExceeded,
    JobLeaseLost,
    classify_job_exception,
)
from app.jobs_v22.executor import ProspectV22Executor, UnavailableV22Executor
from app.jobs_v22.models import JobErrorState, utc_now
from app.jobs_v22.reconciler import reconcile_v22_jobs
from app.jobs_v22.prospect_report_pipeline import PublicProspectReportPipeline
from app.jobs_v22.result_persistence import SupabaseResultPersister
from app.jobs_v22.store import DurableJobStore
from app.jobs_v22.serp_market_stage import build_serp_market_stage
from app.jobs_v22.site_inventory_stage import build_site_inventory_stage
from app.preflight_v22.fetcher import BoundedHomepageFetcher


logger = logging.getLogger(__name__)


# ARQ's default StreamHandler writes ordinary INFO events to stderr. Railway then
# labels healthy worker activity as error-level output. Keep the same concise ARQ
# format, but send both ARQ and application logs to stdout so platform severity is
# meaningful.
ARQ_LOG_CONFIG: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "worker": {
            "format": "%(asctime)s: %(message)s",
            "datefmt": "%H:%M:%S",
        },
    },
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "worker",
            "level": "INFO",
        },
    },
    "root": {"handlers": ["stdout"], "level": "INFO"},
    "loggers": {
        "arq": {
            "handlers": ["stdout"],
            "level": "INFO",
            "propagate": False,
        },
        "httpx": {"level": "WARNING"},
    },
}


def retry_delay_seconds(job_id: UUID, attempt_count: int) -> int:
    """Bounded exponential delay with stable per-job jitter."""

    base = min(5 * (2 ** max(attempt_count - 1, 0)), 60)
    jitter = hashlib.sha256(f"{job_id}:{attempt_count}".encode()).digest()[0] % 4
    return base + jitter


async def execute_v22_job(ctx: dict[str, Any], job_id_value: str, run_generation: int) -> None:
    """Execute one physical run while Redis controls logical state and idempotency."""

    job_id = UUID(job_id_value)
    store: DurableJobStore = ctx["store"]
    state = await store.require_state(job_id)
    if state.terminal or state.run_generation != run_generation:
        return

    lease_token = str(uuid4())
    lease_seconds = int(ctx.get("lease_seconds", settings.V22_JOB_LEASE_SECONDS))
    if not await store.acquire_lease(
        job_id,
        generation=run_generation,
        token=lease_token,
        ttl_seconds=lease_seconds,
    ):
        return

    heartbeat_stop = asyncio.Event()
    heartbeat_lost = asyncio.Event()
    heartbeat_task: asyncio.Task[None] | None = None

    async def maintain_heartbeat() -> None:
        interval = int(ctx.get("heartbeat_seconds", settings.V22_JOB_HEARTBEAT_SECONDS))
        while True:
            try:
                await asyncio.wait_for(heartbeat_stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                refreshed = await store.refresh_lease(
                    job_id,
                    generation=run_generation,
                    token=lease_token,
                    ttl_seconds=lease_seconds,
                )
                if not refreshed:
                    heartbeat_lost.set()
                    return
                await store.heartbeat(job_id, generation=run_generation, now=utc_now())
            except JobLeaseLost:
                heartbeat_lost.set()
                return
            except Exception as exc:
                logger.warning(
                    "v2.2 heartbeat deferred job_id_suffix=%s error=%s",
                    str(job_id)[-8:],
                    type(exc).__name__,
                )

    heartbeat_task = asyncio.create_task(maintain_heartbeat())
    try:
        await _execute_with_lease(
            ctx,
            store=store,
            state=state,
            job_id=job_id,
            run_generation=run_generation,
            heartbeat_lost=heartbeat_lost,
        )
    finally:
        heartbeat_stop.set()
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        await store.release_lease(job_id, generation=run_generation, token=lease_token)


async def _execute_with_lease(
    ctx: dict[str, Any],
    *,
    store: DurableJobStore,
    state,
    job_id: UUID,
    run_generation: int,
    heartbeat_lost: asyncio.Event,
) -> None:
    """Run the task only while this physical generation owns its lease."""

    ledger = JobCostLedger(
        ctx["redis"],
        prefix=store.keys.prefix,
        job_id=job_id,
        ttl_seconds=int(ctx.get("state_ttl_seconds", settings.V22_JOB_STATE_TTL_SECONDS)),
        pricing=ctx.get("cost_pricing") or pricing_catalog_from_settings(settings),
        job_created_at=state.created_at,
    )
    await ledger.ensure()

    now = utc_now()
    if now >= state.deadline_at:
        await _finish_failure(
            ctx,
            state,
            JobDeadlineExceeded(),
            run_generation=run_generation,
            cost_counters=(await ledger.snapshot()).root,
        )
        return

    request = await store.get_request(job_id)
    if request is None:
        exc: BaseException = DeterministicJobError(
            "JOB_REQUEST_MISSING",
            "The persisted analysis request is unavailable.",
        )
        await _finish_failure(
            ctx,
            state,
            exc,
            run_generation=run_generation,
            cost_counters=(await ledger.snapshot()).root,
        )
        return

    attempt_count = state.attempt_count + 1
    started = utc_now()
    running = await store.transition(
        job_id,
        status="running",
        stage="collecting_site",
        progress=max(state.progress, 1),
        message="Analysis is running.",
        now=started,
        attempt_count=attempt_count,
        heartbeat_at=started,
        expected_generation=run_generation,
    )
    if not running.applied:
        return
    await _notify_state(ctx, job_id)

    checkpoints = JobCheckpoints(
        ctx["redis"],
        prefix=store.keys.prefix,
        ttl_seconds=int(ctx.get("state_ttl_seconds", settings.V22_JOB_STATE_TTL_SECONDS)),
        run_generation=run_generation,
    )
    executor = ctx["executor"]
    attempt_started = monotonic()
    try:
        executor_request = request
        if request.get("schema_version") == "v22_analysis_request_envelope_v1":
            envelope = AnalysisRequestEnvelope.model_validate_json(canonical_json_bytes(request))
            executor_request = envelope
        remaining = max((state.deadline_at - utc_now()).total_seconds(), 0.001)
        report = await asyncio.wait_for(
            executor.execute(
                job_id=job_id,
                request=executor_request,
                submitted_at=state.created_at,
                checkpoints=checkpoints,
                cost_ledger=ledger,
            ),
            timeout=remaining,
        )
        if heartbeat_lost.is_set():
            raise JobLeaseLost()
    except TimeoutError:
        await ledger.record_job_attempt(
            active_elapsed_ms=max(int((monotonic() - attempt_started) * 1000), 0)
        )
        await _finish_failure(
            ctx,
            running.state,
            JobDeadlineExceeded(),
            run_generation=run_generation,
            cost_counters=(await ledger.snapshot()).root,
        )
        return
    except asyncio.CancelledError:
        await ledger.record_job_attempt(
            active_elapsed_ms=max(int((monotonic() - attempt_started) * 1000), 0)
        )
        logger.info("v2.2 worker execution cancelled job_id=%s", job_id)
        raise
    except Exception as exc:  # ARQ must receive Retry for classified transient failures.
        await ledger.record_job_attempt(
            active_elapsed_ms=max(int((monotonic() - attempt_started) * 1000), 0)
        )
        await _finish_failure(
            ctx,
            running.state,
            exc,
            run_generation=run_generation,
            cost_counters=(await ledger.snapshot()).root,
        )
        return

    await ledger.record_job_attempt(
        active_elapsed_ms=max(int((monotonic() - attempt_started) * 1000), 0)
    )
    cost_counters = (await ledger.snapshot()).root
    completed = await store.transition(
        job_id,
        status="succeeded",
        stage="completed",
        progress=100,
        message="Analysis complete.",
        now=utc_now(),
        report=report,
        cost_counters=cost_counters,
        expected_generation=run_generation,
    )
    if completed.applied:
        await _notify_state(ctx, job_id)
        await _enqueue_cost_summary(ctx, completed.state, job_kind="prospect_report")


async def _finish_failure(
    ctx: dict[str, Any],
    state,
    exc: BaseException,
    *,
    run_generation: int,
    cost_counters: dict[str, int] | None = None,
) -> None:
    store: DurableJobStore = ctx["store"]
    failure = classify_job_exception(exc)
    max_attempts = int(ctx.get("max_attempts", settings.V22_JOB_MAX_ATTEMPTS))
    attempt_count = max(state.attempt_count, 1)

    if isinstance(exc, JobLeaseLost):
        return

    if failure.retryable and attempt_count < max_attempts:
        queued = await store.transition(
            state.job_id,
            status="queued",
            stage="queued",
            progress=state.progress,
            message="A temporary issue occurred. The task will retry automatically.",
            now=utc_now(),
            attempt_count=attempt_count,
            cost_counters=cost_counters,
            expected_generation=run_generation,
        )
        if queued.applied:
            await _notify_state(ctx, state.job_id)
        raise Retry(defer=retry_delay_seconds(state.job_id, attempt_count))

    error_code = "JOB_RETRY_EXHAUSTED" if failure.retryable else failure.error_code
    user_message = (
        "The analysis could not be completed after multiple attempts. Please retry later."
        if failure.retryable
        else failure.user_message
    )
    error = JobErrorState(
        error_code=error_code,
        user_message=user_message,
        retryable=failure.retryable,
        stage="failed",
        diagnostic_id=uuid4(),
    )
    failed = await store.transition(
        state.job_id,
        status="failed",
        stage="failed",
        progress=state.progress,
        message=user_message,
        now=utc_now(),
        attempt_count=attempt_count,
        error=error,
        cost_counters=cost_counters,
        expected_generation=run_generation,
    )
    if failed.applied:
        await _notify_state(ctx, state.job_id)
        await _enqueue_cost_summary(ctx, failed.state, job_kind="prospect_report")


async def _enqueue_cost_summary(ctx: dict[str, Any], state, *, job_kind: str) -> None:
    outbox: CostSummaryOutbox | None = ctx.get("cost_summary_outbox")
    if outbox is None or not state.terminal or state.completed_at is None:
        return
    try:
        counters = await JobCostLedger(
            ctx["redis"],
            prefix=ctx["store"].keys.prefix,
            job_id=state.job_id,
            ttl_seconds=int(ctx.get("state_ttl_seconds", settings.V22_JOB_STATE_TTL_SECONDS)),
            pricing=ctx.get("cost_pricing") or pricing_catalog_from_settings(settings),
            job_created_at=state.created_at,
        ).snapshot()
        summary = CostSummaryRecord(
            job_id=state.job_id,
            case_id=state.case_id,
            job_kind=job_kind,
            status=state.status,
            attempt_count=state.attempt_count,
            ledger_revision=counters.root["cost_ledger_revision"],
            cost_counters=counters,
            started_at=state.created_at,
            completed_at=state.completed_at,
        )
        await outbox.enqueue(summary)
        await outbox.sync(state.job_id)
    except Exception as exc:
        logger.warning(
            "v2.2 cost summary deferred job_id_suffix=%s error=%s",
            str(state.job_id)[-8:],
            type(exc).__name__,
        )


async def reconcile_v22_cost_summaries(ctx: dict[str, Any]) -> None:
    outbox: CostSummaryOutbox | None = ctx.get("cost_summary_outbox")
    if outbox is not None:
        await outbox.flush(limit=50)


async def _notify_state(ctx: dict[str, Any], job_id: UUID) -> None:
    synchronizer: CallbackSynchronizer | None = ctx.get("callback_synchronizer")
    if synchronizer is not None:
        try:
            await synchronizer.sync(job_id)
        except Exception as exc:  # Callback delivery must never change the analysis outcome.
            logger.warning(
                "v2.2 callback synchronization deferred job_id_suffix=%s error=%s",
                str(job_id)[-8:],
                type(exc).__name__,
            )


def _secret_value(value: SecretStr | str) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else value


async def on_startup(ctx: dict[str, Any]) -> None:
    pool = ctx["redis"]
    if settings.V22_GSC_SYNC_ENABLED:
        gsc_client = httpx.AsyncClient(timeout=20, follow_redirects=False)
        ctx["gsc_http_client"] = gsc_client
        ctx["gsc_sync_repository"] = SyncRepository(settings.V22_SUPABASE_URL, _secret_value(settings.V22_SUPABASE_SERVICE_ROLE_KEY), gsc_client)
        ctx["gsc_token_broker"] = TokenBroker(settings.V22_GOOGLE_BROKER_ORIGIN, _secret_value(settings.V22_GOOGLE_BROKER_SECRET), gsc_client)
        ctx["gsc_provider"] = GscProvider(gsc_client)
        ctx["gsc_queue_name"] = settings.V22_QUEUE_NAME
    if settings.V22_GA4_SYNC_ENABLED:
        ga4_client = httpx.AsyncClient(timeout=20, follow_redirects=False)
        ctx["ga4_http_client"] = ga4_client
        ctx["ga4_sync_repository"] = SyncRepository(settings.V22_SUPABASE_URL,
            _secret_value(settings.V22_SUPABASE_SERVICE_ROLE_KEY), ga4_client, source="ga4")
        ctx["ga4_token_broker"] = TokenBroker(settings.V22_GOOGLE_BROKER_ORIGIN,
            _secret_value(settings.V22_GOOGLE_BROKER_SECRET), ga4_client, source="ga4")
        ctx["ga4_provider"] = Ga4Provider(ga4_client)
        ctx["ga4_queue_name"] = settings.V22_QUEUE_NAME
    if settings.V22_GBP_SYNC_ENABLED:
        gbp_client = httpx.AsyncClient(timeout=20, follow_redirects=False)
        ctx["gbp_http_client"] = gbp_client
        ctx["gbp_sync_repository"] = SyncRepository(
            settings.V22_SUPABASE_URL,
            _secret_value(settings.V22_SUPABASE_SERVICE_ROLE_KEY),
            gbp_client,
            source="gbp",
        )
        ctx["gbp_token_broker"] = TokenBroker(
            settings.V22_GOOGLE_BROKER_ORIGIN,
            _secret_value(settings.V22_GOOGLE_BROKER_SECRET),
            gbp_client,
            source="gbp",
        )
        ctx["gbp_provider"] = GbpProvider(gbp_client)
        ctx["gbp_queue_name"] = settings.V22_QUEUE_NAME
    storage_key = _secret_value(settings.V22_SUPABASE_SERVICE_ROLE_KEY)
    if settings.V22_SUPABASE_URL and storage_key:
        cleanup_client = httpx.AsyncClient(timeout=20, follow_redirects=False)
        ctx["gbp_cleanup_http_client"] = cleanup_client
        ctx["gbp_cleanup_repository"] = SyncRepository(
            settings.V22_SUPABASE_URL,
            storage_key,
            cleanup_client,
            source="gbp",
        )
        cost_http_client = httpx.AsyncClient(
            timeout=settings.V22_RESULT_PERSISTENCE_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
        ctx["cost_http_client"] = cost_http_client
        ctx["cost_summary_outbox"] = CostSummaryOutbox(
            pool,
            prefix=settings.V22_REDIS_PREFIX,
            ttl_seconds=settings.V22_JOB_STATE_TTL_SECONDS,
            persister=CostSummaryPersister(
                url=settings.V22_SUPABASE_URL,
                service_role_key=storage_key,
                http_client=cost_http_client,
            ),
        )
    ctx["store"] = DurableJobStore(
        pool,
        prefix=settings.V22_REDIS_PREFIX,
        state_ttl_seconds=settings.V22_JOB_STATE_TTL_SECONDS,
        job_timeout_seconds=settings.V22_JOB_TIMEOUT_SECONDS,
    )
    ctx["max_attempts"] = settings.V22_JOB_MAX_ATTEMPTS
    ctx["heartbeat_seconds"] = settings.V22_JOB_HEARTBEAT_SECONDS
    ctx["lease_seconds"] = settings.V22_JOB_LEASE_SECONDS
    ctx["state_ttl_seconds"] = settings.V22_JOB_STATE_TTL_SECONDS
    ctx["competitor_state_ttl_seconds"] = settings.V22_COMPETITOR_STATE_TTL_SECONDS
    ctx["cost_pricing"] = pricing_catalog_from_settings(settings)
    discovery_store = CompetitorDiscoveryStore(
        pool,
        prefix=settings.V22_REDIS_PREFIX,
        state_ttl_seconds=settings.V22_COMPETITOR_STATE_TTL_SECONDS,
    )
    market_store = SharedMarketSnapshotStore(
        pool,
        prefix=settings.V22_REDIS_PREFIX,
        ttl_seconds=settings.V22_COMPETITOR_MARKET_TTL_SECONDS,
    )
    circuit_breaker = RedisCircuitBreaker(
        pool,
        prefix=settings.V22_REDIS_PREFIX,
        failure_threshold=settings.V22_PROVIDER_CIRCUIT_FAILURE_THRESHOLD,
        window_seconds=settings.V22_PROVIDER_CIRCUIT_WINDOW_SECONDS,
    )
    ctx["provider_circuit_breaker"] = circuit_breaker
    ctx["competitor_discovery_store"] = discovery_store
    ctx["competitor_discovery_service"] = CompetitorDiscoveryService(
        market_stage=build_serp_market_stage(settings, circuit_breaker=circuit_breaker),
        market_store=market_store,
        supplemental_validator=SupplementalHomepageValidator(
            BoundedHomepageFetcher(
                connect_timeout=settings.V22_COMPETITOR_CONNECT_TIMEOUT_SECONDS,
                read_timeout=settings.V22_COMPETITOR_READ_TIMEOUT_SECONDS,
                total_timeout=settings.V22_COMPETITOR_TOTAL_TIMEOUT_SECONDS,
                max_redirects=settings.V22_PREFLIGHT_MAX_REDIRECTS,
                max_response_bytes=settings.V22_COMPETITOR_MAX_RESPONSE_BYTES,
            )
        ),
    )
    if settings.V22_ANALYZE_ENABLED:
        site_stage = build_site_inventory_stage(settings)
        copy_http_client = httpx.AsyncClient(
            timeout=settings.V22_DIFY_TIMEOUT_SECONDS
        )
        ctx["copy_http_client"] = copy_http_client
        result_http_client = httpx.AsyncClient(
            timeout=settings.V22_RESULT_PERSISTENCE_TIMEOUT_SECONDS
        )
        ctx["result_http_client"] = result_http_client
        ctx["executor"] = ProspectV22Executor(
            discovery_store=discovery_store,
            market_store=market_store,
            site_stage=site_stage,
            competitor_stage=CheckpointedCompetitorCollectionStage(
                site_stage=CheckpointedCompetitorSiteStage(site_stage),
                profile_stage=build_public_profile_stage(settings),
            ),
            report_pipeline=PublicProspectReportPipeline(
                copy_provider=DifyControlledCopyProvider(
                    api_key=_secret_value(settings.V22_DIFY_API_KEY),
                    api_url=settings.V22_DIFY_API_URL,
                    model_version=settings.V22_DIFY_COPY_MODEL_VERSION,
                    http_client=copy_http_client,
                    circuit_breaker=circuit_breaker,
                )
            ),
            result_persister=SupabaseResultPersister(
                url=settings.V22_SUPABASE_URL,
                service_role_key=_secret_value(settings.V22_SUPABASE_SERVICE_ROLE_KEY),
                http_client=result_http_client,
            ),
        )
    else:
        ctx["executor"] = UnavailableV22Executor()
    callback_url = settings.V22_CALLBACK_URL
    callback_secret = _secret_value(settings.V22_CALLBACK_SECRET)
    if callback_url and callback_secret:
        http_client = httpx.AsyncClient(timeout=settings.V22_CALLBACK_TIMEOUT_SECONDS)
        ctx["callback_http_client"] = http_client
        sender = SignedCallbackClient(
            url=callback_url,
            secret=callback_secret,
            http_client=http_client,
        )
        ctx["callback_synchronizer"] = CallbackSynchronizer(ctx["store"], sender)


async def on_shutdown(ctx: dict[str, Any]) -> None:
    if ctx.get("gsc_http_client") is not None:
        await ctx["gsc_http_client"].aclose()
    if ctx.get("ga4_http_client") is not None:
        await ctx["ga4_http_client"].aclose()
    if ctx.get("gbp_http_client") is not None:
        await ctx["gbp_http_client"].aclose()
    if ctx.get("gbp_cleanup_http_client") is not None:
        await ctx["gbp_cleanup_http_client"].aclose()
    if ctx.get("cost_http_client") is not None:
        await ctx["cost_http_client"].aclose()
    http_client: httpx.AsyncClient | None = ctx.get("callback_http_client")
    if http_client is not None:
        await http_client.aclose()
    copy_http_client: httpx.AsyncClient | None = ctx.get("copy_http_client")
    if copy_http_client is not None:
        await copy_http_client.aclose()
    result_http_client: httpx.AsyncClient | None = ctx.get("result_http_client")
    if result_http_client is not None:
        await result_http_client.aclose()


def _redis_settings() -> RedisSettings:
    dsn = settings.V22_REDIS_URL.get_secret_value()
    return RedisSettings.from_dsn(dsn) if dsn else RedisSettings()


class WorkerSettings:
    functions = [
        func(execute_v22_gsc_sync, name="execute_v22_gsc_sync", max_tries=1, timeout=270, keep_result=0),
        func(execute_v22_ga4_sync, name="execute_v22_ga4_sync", max_tries=1, timeout=270, keep_result=0),
        func(execute_v22_gbp_sync, name="execute_v22_gbp_sync", max_tries=1, timeout=270, keep_result=0),
        func(
            execute_v22_job,
            name="execute_v22_job",
            max_tries=settings.V22_JOB_MAX_ATTEMPTS,
            timeout=settings.V22_JOB_TIMEOUT_SECONDS,
            keep_result=settings.V22_JOB_STATE_TTL_SECONDS,
        ),
        func(
            execute_v22_competitor_discovery,
            name="execute_v22_competitor_discovery",
            max_tries=settings.V22_JOB_MAX_ATTEMPTS,
            timeout=settings.V22_JOB_TIMEOUT_SECONDS,
            keep_result=settings.V22_COMPETITOR_STATE_TTL_SECONDS,
        ),
    ]
    cron_jobs = [
        cron(reconcile_v22_gsc_syncs, name="reconcile_v22_gsc_syncs", second={15, 45}, unique=True, max_tries=1),
        cron(reconcile_v22_ga4_syncs, name="reconcile_v22_ga4_syncs", second={20, 50}, unique=True, max_tries=1),
        cron(reconcile_v22_gbp_syncs, name="reconcile_v22_gbp_syncs", second={25, 55}, unique=True, max_tries=1),
        cron(cleanup_v22_gbp_content, name="cleanup_v22_gbp_content", minute={7, 37}, second=5, unique=True, max_tries=1),
        cron(
            reconcile_v22_cost_summaries,
            name="reconcile_v22_cost_summaries",
            minute=set(range(60)),
            second=35,
            unique=True,
            max_tries=1,
        ),
        cron(
            reconcile_v22_jobs,
            name="reconcile_v22_jobs",
            second={0, 30},
            unique=True,
            max_tries=1,
        ),
        cron(
            reconcile_v22_competitor_discoveries,
            name="reconcile_v22_competitor_discoveries",
            second={10, 40},
            unique=True,
            max_tries=1,
        ),
    ]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = _redis_settings()
    queue_name = settings.V22_QUEUE_NAME
    max_jobs = settings.V22_WORKER_CONCURRENCY
    job_timeout = settings.V22_JOB_TIMEOUT_SECONDS
    max_tries = settings.V22_JOB_MAX_ATTEMPTS
    keep_result = settings.V22_JOB_STATE_TTL_SECONDS
    health_check_interval = settings.V22_JOB_HEARTBEAT_SECONDS
    health_check_key = f"{settings.V22_REDIS_PREFIX}:worker-health"
    retry_jobs = True
    job_completion_wait = 20
