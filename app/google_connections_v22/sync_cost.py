"""Shared cost-ledger helpers for database-controlled Google sync attempts."""

from __future__ import annotations

from datetime import datetime, timezone
from time import monotonic
from typing import Any
from uuid import UUID

from app.core.config import settings
from app.jobs_v22.cost_ledger import JobCostLedger


def build_sync_cost_ledger(
    ctx: dict[str, Any], job: dict[str, Any]
) -> JobCostLedger | None:
    pricing = ctx.get("cost_pricing")
    if pricing is None:
        return None
    created_at = job.get("created_at")
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            created_at = None
    if not isinstance(created_at, datetime) or created_at.tzinfo is None:
        created_at = datetime.now(timezone.utc)
    return JobCostLedger(
        ctx["redis"],
        prefix=ctx.get("cost_prefix", settings.V22_REDIS_PREFIX),
        job_id=UUID(str(job["id"])),
        ttl_seconds=int(
            ctx.get("state_ttl_seconds", settings.V22_JOB_STATE_TTL_SECONDS)
        ),
        pricing=pricing,
        job_created_at=created_at,
    )


def attempt_timer() -> float:
    return monotonic()


async def finish_attempt(
    ledger: JobCostLedger | None, started: float
) -> dict[str, int] | None:
    if ledger is None:
        return None
    await ledger.record_job_attempt(
        active_elapsed_ms=max(int((monotonic() - started) * 1000), 0)
    )
    return (await ledger.snapshot()).root
