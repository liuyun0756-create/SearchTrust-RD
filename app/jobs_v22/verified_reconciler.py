"""Bounded expiry of paid Verified jobs that never reached Redis."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any
from uuid import UUID

import httpx

from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.models import utc_now
from app.jobs_v22.verified_input_resolver import VerifiedRpcClient


logger = logging.getLogger(__name__)
MAX_EXPIRED_JOBS = 100
MAX_RESPONSE_BYTES = 64 * 1024


class SupabaseVerifiedOrphanReconciler:
    """Call the database-owned, exactly-once compensation operation."""

    def __init__(
        self,
        *,
        url: str,
        service_role_key: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        self.rpc = VerifiedRpcClient(
            url=url,
            service_role_key=service_role_key,
            http_client=http_client,
            max_response_bytes=MAX_RESPONSE_BYTES,
            prefix="V22_VERIFIED_ORPHAN_RECONCILIATION",
            invalid_retryable=True,
        )

    async def expire(self, *, now: datetime) -> list[UUID]:
        try:
            rows = await self.rpc.post(
                "expire_v22_stale_verified_jobs",
                {"p_now": now.isoformat(), "p_limit": MAX_EXPIRED_JOBS},
            )
            if (
                not isinstance(rows, list)
                or len(rows) > MAX_EXPIRED_JOBS
                or any(
                    not isinstance(row, dict)
                    or set(row) != {"job_id"}
                    or not isinstance(row["job_id"], str)
                    for row in rows
                )
            ):
                raise ValueError
            job_ids = [UUID(row["job_id"]) for row in rows]
            if len(set(job_ids)) != len(job_ids):
                raise ValueError
            return job_ids
        except (
            DeterministicJobError,
            TransientJobError,
            TypeError,
            ValueError,
            OverflowError,
        ) as exc:
            logger.warning(
                "v2.2 Verified orphan reconciliation deferred error=%s",
                type(exc).__name__,
            )
            return []


async def reconcile_v22_verified_orphans(ctx: dict[str, Any]) -> None:
    reconciler: SupabaseVerifiedOrphanReconciler | None = ctx.get(
        "verified_orphan_reconciler"
    )
    if reconciler is not None:
        await reconciler.expire(now=utc_now())
