"""Dispatch persisted explicit requests only; never schedule new collections."""
import json
import logging
from uuid import UUID

from app.google_connections_v22.sync_cost import build_sync_cost_ledger
from app.report_v22.ga4_collection_executor import execute_ga4_collection

logger=logging.getLogger(__name__)


async def reconcile_v22_ga4_collections(ctx):
    repository=ctx.get("ga4_comparison_collection_repository")
    if repository is None:return
    try:
        ids=json.loads(await repository._rpc("pending_v22_ga4_collections",{}))
        if not isinstance(ids,list) or len(ids)>20:raise ValueError("invalid pending list")
        ids=[str(UUID(i)) for i in ids]
        for job_id in ids:
            await ctx["redis"].enqueue_job("execute_v22_ga4_collection",job_id,
                _job_id=f"ga4-comparison-collection:{job_id}",_queue_name=ctx["ga4_comparison_collection_queue"])
    except Exception:
        logger.warning("Comparison collection dispatch deferred")


async def execute_v22_ga4_collection(ctx,job_id):
    repository=ctx.get("ga4_comparison_collection_repository")
    if repository is None:return
    # Returning nothing to ARQ avoids retaining private result bodies in Redis.
    await execute_ga4_collection(repository,UUID(job_id),provider=ctx["ga4_comparison_collection_provider"],
        token_broker=ctx["ga4_comparison_collection_broker"],
        ledger_factory=lambda job:build_sync_cost_ledger(ctx,{"id":str(job.id),"created_at":job.created_at}))
