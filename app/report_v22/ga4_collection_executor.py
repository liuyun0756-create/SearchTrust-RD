"""Single-attempt exact GA4 execution for explicit durable requests only."""
import asyncio
import logging
from uuid import UUID

from app.google_connections_v22.gsc import SyncError
from app.jobs_v22.cost_ledger import CostLimitExceeded
from app.report_v22.ga4_comparison_collection import collect_ga4_comparison
from app.report_v22.ga4_collection_storage import Ga4CollectionRepository

logger = logging.getLogger(__name__)


async def execute_ga4_collection(repository: Ga4CollectionRepository, collection_id: UUID, *, provider,
                                 token_broker, ledger_factory):
    job = await repository.claim_collection(collection_id)
    if job is None:
        return None
    try:
        async with asyncio.timeout(240):
            ledger = ledger_factory(job)
            if ledger is None or str(ledger.job_id) != str(job.id):
                raise SyncError("SYNC_COST_LEDGER_REQUIRED")
            await ledger.ensure()
            token = await token_broker.access_token(str(job.connection_id))
            try:
                collection = await collect_ga4_comparison(job.record, action_id=job.selection.action_id,
                    days=job.selection.days, binding_id=job.selection.binding_id, scope=job.plan.scope,
                    at=job.plan.prepared_at, provider=provider, token=token, cost_ledger=ledger)
            finally:
                del token
    except asyncio.CancelledError:
        # The lease can expire but cannot be reused for a second attempt.
        raise
    except Exception as error:
        code = (error.code if isinstance(error, SyncError) else "SYNC_COST_LIMIT" if isinstance(error, CostLimitExceeded)
                else "SYNC_TIMEOUT" if isinstance(error, TimeoutError) else "SYNC_FAILED")
        await repository.fail_collection(job, code)
        logger.warning("GA4 comparison collection attempt ended")
        return await repository.read_collection(user_id=job.user_id, case_id=job.case_id, collection_id=job.id)
    # Uncertain persistence is recovered by read/lease expiry, not by refetch.
    return await repository.finish_collection(job, collection)
