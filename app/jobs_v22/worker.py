"""ARQ worker entrypoint for isolated SearchTrust v2.2 durable tasks."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from arq import Retry
from arq.connections import RedisSettings
from arq.cron import cron
from arq.worker import func
import httpx
from pydantic import SecretStr

from app.core.config import settings
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
from app.jobs_v22.copy_provider import DifyControlledCopyProvider
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError, classify_job_exception
from app.jobs_v22.executor import ProspectV22Executor, UnavailableV22Executor
from app.jobs_v22.models import JobErrorState, utc_now
from app.jobs_v22.reconciler import reconcile_v22_jobs
from app.jobs_v22.prospect_report_pipeline import PublicProspectReportPipeline
from app.jobs_v22.store import DurableJobStore
from app.jobs_v22.serp_market_stage import build_serp_market_stage
from app.jobs_v22.site_inventory_stage import build_site_inventory_stage
from app.preflight_v22.fetcher import BoundedHomepageFetcher


logger = logging.getLogger(__name__)


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

    request = await store.get_request(job_id)
    if request is None:
        exc: BaseException = DeterministicJobError(
            "JOB_REQUEST_MISSING",
            "The persisted analysis request is unavailable.",
        )
        await _finish_failure(ctx, state, exc)
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
    )
    if not running.applied:
        return
    await _notify_state(ctx, job_id)

    checkpoints = JobCheckpoints(
        ctx["redis"],
        prefix=store.keys.prefix,
        ttl_seconds=int(ctx.get("state_ttl_seconds", settings.V22_JOB_STATE_TTL_SECONDS)),
    )
    executor = ctx["executor"]
    try:
        executor_request = request
        if request.get("schema_version") == "v22_analysis_request_envelope_v1":
            envelope = AnalysisRequestEnvelope.model_validate_json(canonical_json_bytes(request))
            executor_request = envelope
        report = await executor.execute(
            job_id=job_id,
            request=executor_request,
            submitted_at=state.created_at,
            checkpoints=checkpoints,
        )
    except asyncio.CancelledError:
        logger.info("v2.2 worker execution cancelled job_id=%s", job_id)
        raise
    except Exception as exc:  # ARQ must receive Retry for classified transient failures.
        await _finish_failure(ctx, running.state, exc)
        return


    completed = await store.transition(
        job_id,
        status="succeeded",
        stage="completed",
        progress=100,
        message="Analysis complete.",
        now=utc_now(),
        report=report,
    )
    if completed.applied:
        await _notify_state(ctx, job_id)


async def _finish_failure(ctx: dict[str, Any], state, exc: BaseException) -> None:
    store: DurableJobStore = ctx["store"]
    failure = classify_job_exception(exc)
    max_attempts = int(ctx.get("max_attempts", settings.V22_JOB_MAX_ATTEMPTS))
    attempt_count = max(state.attempt_count, 1)

    if failure.retryable and attempt_count < max_attempts:
        queued = await store.transition(
            state.job_id,
            status="queued",
            stage="queued",
            progress=state.progress,
            message="A temporary issue occurred. The task will retry automatically.",
            now=utc_now(),
            attempt_count=attempt_count,
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
    )
    if failed.applied:
        await _notify_state(ctx, state.job_id)


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
    ctx["store"] = DurableJobStore(
        pool,
        prefix=settings.V22_REDIS_PREFIX,
        state_ttl_seconds=settings.V22_JOB_STATE_TTL_SECONDS,
    )
    ctx["max_attempts"] = settings.V22_JOB_MAX_ATTEMPTS
    ctx["state_ttl_seconds"] = settings.V22_JOB_STATE_TTL_SECONDS
    ctx["competitor_state_ttl_seconds"] = settings.V22_COMPETITOR_STATE_TTL_SECONDS
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
    ctx["competitor_discovery_store"] = discovery_store
    ctx["competitor_discovery_service"] = CompetitorDiscoveryService(
        market_stage=build_serp_market_stage(settings),
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
                )
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
    http_client: httpx.AsyncClient | None = ctx.get("callback_http_client")
    if http_client is not None:
        await http_client.aclose()
    copy_http_client: httpx.AsyncClient | None = ctx.get("copy_http_client")
    if copy_http_client is not None:
        await copy_http_client.aclose()


def _redis_settings() -> RedisSettings:
    dsn = settings.V22_REDIS_URL.get_secret_value()
    return RedisSettings.from_dsn(dsn) if dsn else RedisSettings()


class WorkerSettings:
    functions = [
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
