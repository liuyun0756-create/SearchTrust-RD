"""Recover prepared tasks that never reached Redis, using SQL's own deadline."""
import logging
from uuid import UUID

from app.jobs_v22.verified_input_resolver import VerifiedRpcClient

logger = logging.getLogger(__name__)


class CapabilityOrphanReconciler:
    def __init__(self, *, url, service_role_key, http_client):
        self.rpc = VerifiedRpcClient(
            url=url, service_role_key=service_role_key, http_client=http_client,
            max_response_bytes=64 * 1024, prefix="V22_CAPABILITY_EXPIRY", invalid_retryable=True,
        )

    async def expire(self):
        rows = await self.rpc.post("expire_v22_prepared_capability_jobs", {"p_limit": 100})
        try:
            if not isinstance(rows, list) or len(rows) > 100:
                raise ValueError()
            if any(not isinstance(r, dict) or set(r) != {"job_id"} or not isinstance(r["job_id"], str) for r in rows):
                raise ValueError()
            ids = [UUID(r["job_id"]) for r in rows]
            if len(set(ids)) != len(ids):
                raise ValueError()
            return ids
        except (ValueError, TypeError):
            raise self.rpc.invalid() from None


async def reconcile_v22_capability_orphans(ctx):
    reconciler = ctx.get("capability_orphan_reconciler")
    if reconciler is not None:
        try:
            await reconciler.expire()
        except Exception as exc:
            logger.warning("capability deadline reconciliation deferred error=%s", type(exc).__name__)
