"""Worker execution for durable competitor-discovery tasks."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from arq import Retry

from app.api.v2.competitor_models import CompetitorDiscoveryError, CompetitorDiscoveryRequest
from app.competitors_v22.store import CompetitorDiscoveryStore
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.cost_ledger import JobCostLedger
from app.jobs_v22.cost_models import pricing_catalog_from_settings
from app.jobs_v22.cost_models import CostSummaryRecord
from app.jobs_v22.cost_persistence import CostSummaryOutbox
from app.core.config import settings
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError, classify_job_exception


logger = logging.getLogger(__name__)


def _now(ctx: dict[str, Any]) -> datetime:
    clock = ctx.get("clock")
    return clock() if clock is not None else datetime.now(timezone.utc)


def _retry_delay_seconds(job_id: UUID, attempt_count: int) -> int:
    base = min(5 * (2 ** max(attempt_count - 1, 0)), 60)
    jitter = hashlib.sha256(f"{job_id}:{attempt_count}".encode()).digest()[0] % 4
    return base + jitter


async def execute_v22_competitor_discovery(
    ctx: dict[str, Any], discovery_job_id_value: str, run_generation: int
) -> None:
    discovery_job_id = UUID(discovery_job_id_value)
    store: CompetitorDiscoveryStore = ctx["competitor_discovery_store"]
    state = await store.require_state(discovery_job_id)
    if state.terminal or state.run_generation != run_generation:
        return

    ledger = JobCostLedger(
        ctx["redis"],
        prefix=store.keys.prefix,
        job_id=discovery_job_id,
        ttl_seconds=int(ctx.get("competitor_state_ttl_seconds", store.state_ttl_seconds)),
        pricing=ctx.get("cost_pricing") or pricing_catalog_from_settings(settings),
        job_created_at=state.created_at,
    )
    await ledger.ensure()

    attempt_count = state.attempt_count + 1
    started = _now(ctx)
    running = await store.transition(
        discovery_job_id,
        status="running",
        stage="collecting_market",
        progress=max(state.progress, 1),
        message="Collecting market search results.",
        now=started,
        attempt_count=attempt_count,
        heartbeat_at=started,
    )
    if not running.applied:
        return

    checkpoints = JobCheckpoints(
        ctx["redis"],
        prefix=store.keys.prefix,
        ttl_seconds=int(
            ctx.get("competitor_state_ttl_seconds", store.state_ttl_seconds)
        ),
    )

    async def progress(stage: str, percent: int, message: str) -> None:
        await store.transition(
            discovery_job_id,
            status="running",
            stage=stage,
            progress=percent,
            message=message,
            now=_now(ctx),
            heartbeat_at=_now(ctx),
        )

    service = ctx["competitor_discovery_service"]
    attempt_started = monotonic()
    try:
        raw_request = await store.get_request(discovery_job_id)
        if raw_request is None:
            raise DeterministicJobError(
                "DISCOVERY_REQUEST_MISSING",
                "The persisted competitor discovery request is unavailable.",
            )
        request = CompetitorDiscoveryRequest.model_validate_json(canonical_json_bytes(raw_request))
        result = await service.discover(
            discovery_job_id=discovery_job_id,
            request=request,
            checkpoints=checkpoints,
            progress=progress,
            cost_ledger=ledger,
        )
    except asyncio.CancelledError:
        await ledger.record_job_attempt(
            active_elapsed_ms=max(int((monotonic() - attempt_started) * 1000), 0)
        )
        logger.info("competitor discovery cancelled job_id=%s", discovery_job_id)
        raise
    except Exception as exc:
        await ledger.record_job_attempt(
            active_elapsed_ms=max(int((monotonic() - attempt_started) * 1000), 0)
        )
        await _finish_failure(
            ctx,
            running.state,
            exc,
            cost_counters=(await ledger.snapshot()).root,
        )
        return

    await ledger.record_job_attempt(
        active_elapsed_ms=max(int((monotonic() - attempt_started) * 1000), 0)
    )
    completed = await store.transition(
        discovery_job_id,
        status="succeeded",
        stage="completed",
        progress=100,
        message="Competitor discovery complete.",
        now=_now(ctx),
        result=result,
        cost_counters=(await ledger.snapshot()).root,
    )
    if completed.applied:
        await _enqueue_cost_summary(ctx, completed.state)


async def _finish_failure(
    ctx: dict[str, Any],
    state,
    exc: BaseException,
    *,
    cost_counters: dict[str, int] | None = None,
) -> None:
    store: CompetitorDiscoveryStore = ctx["competitor_discovery_store"]
    failure = classify_job_exception(exc)
    max_attempts = int(ctx.get("max_attempts", 3))
    attempt_count = max(state.attempt_count, 1)
    logger.warning(
        "competitor discovery attempt failed job_id=%s attempt=%d error_code=%s retryable=%s",
        state.discovery_job_id,
        attempt_count,
        failure.error_code,
        failure.retryable,
    )
    if failure.retryable and attempt_count < max_attempts:
        await store.transition(
            state.discovery_job_id,
            status="queued",
            stage="queued",
            progress=state.progress,
            message="A temporary issue occurred. Competitor discovery will retry automatically.",
            now=_now(ctx),
            attempt_count=attempt_count,
            cost_counters=cost_counters,
        )
        raise Retry(defer=_retry_delay_seconds(state.discovery_job_id, attempt_count))

    error_code = "DISCOVERY_RETRY_EXHAUSTED" if failure.retryable else failure.error_code
    user_message = (
        "Competitor discovery could not be completed after multiple attempts. Please retry later."
        if failure.retryable
        else failure.user_message
    )
    error = CompetitorDiscoveryError(
        error_code=error_code,
        user_message=user_message,
        retryable=failure.retryable,
        stage="failed",
        diagnostic_id=uuid4(),
    )
    failed = await store.transition(
        state.discovery_job_id,
        status="failed",
        stage="failed",
        progress=state.progress,
        message=user_message,
        now=_now(ctx),
        attempt_count=attempt_count,
        error=error,
        cost_counters=cost_counters,
    )
    if failed.applied:
        await _enqueue_cost_summary(ctx, failed.state)


async def _enqueue_cost_summary(ctx: dict[str, Any], state) -> None:
    outbox: CostSummaryOutbox | None = ctx.get("cost_summary_outbox")
    if outbox is None or not state.terminal or state.completed_at is None:
        return
    try:
        ledger = JobCostLedger(
            ctx["redis"],
            prefix=ctx["competitor_discovery_store"].keys.prefix,
            job_id=state.discovery_job_id,
            ttl_seconds=int(
                ctx.get(
                    "competitor_state_ttl_seconds",
                    ctx["competitor_discovery_store"].state_ttl_seconds,
                )
            ),
            pricing=ctx.get("cost_pricing") or pricing_catalog_from_settings(settings),
            job_created_at=state.created_at,
        )
        counters = await ledger.snapshot()
        summary = CostSummaryRecord(
            job_id=state.discovery_job_id,
            case_id=state.case_id,
            job_kind="competitor_discovery",
            status=state.status,
            attempt_count=state.attempt_count,
            ledger_revision=counters.root["cost_ledger_revision"],
            cost_counters=counters,
            started_at=state.created_at,
            completed_at=state.completed_at,
        )
        await outbox.enqueue(summary)
        await outbox.sync(state.discovery_job_id)
    except Exception as exc:
        logger.warning(
            "competitor cost summary deferred job_id_suffix=%s error=%s",
            str(state.discovery_job_id)[-8:],
            type(exc).__name__,
        )
