"""Internal single-attempt execution. Not registered with a scheduler or route."""
import asyncio
import logging
from uuid import UUID

from app.google_connections_v22.gsc import SyncError
from app.jobs_v22.cost_ledger import CostLimitExceeded
from app.report_v22.comparison_collection import collect_gsc_comparison
from app.report_v22.collection_storage import CollectionRepository

logger = logging.getLogger(__name__)


async def execute_collection(repository: CollectionRepository, collection_id: UUID, *, provider, token_broker,
                             ledger_factory):
    """All dependencies are internal. Call only for an explicit persisted request.

    The factory must return the existing logical-job ledger for this job ID,
    never a new random ID. A DB claim is single-use across process restarts.
    """
    job = await repository.claim_collection(collection_id)
    if job is None:
        return None
    try:
        async with asyncio.timeout(240):
            ledger = ledger_factory(job)
            if ledger is None:
                raise SyncError("SYNC_COST_LEDGER_REQUIRED")
            # Do not allow an unrelated ledger to bypass the logical job budget.
            if str(ledger.job_id) != str(job.id):
                raise SyncError("SYNC_COST_LEDGER_REQUIRED")
            await ledger.ensure()
            token = await token_broker.access_token(str(job.connection_id))
            try:
                collection = await collect_gsc_comparison(job.record, action_id=job.selection.action_id,
                    days=job.selection.days, binding_id=job.selection.binding_id, resource_id=job.plan.resource_id,
                    at=job.plan.prepared_at, provider=provider, token=token, cost_ledger=ledger)
            finally:
                del token
    except asyncio.CancelledError:
        # Recovery expires this lease; it never repeats the provider attempt.
        raise
    except Exception as error:
        code = (error.code if isinstance(error, SyncError) else "SYNC_COST_LIMIT" if isinstance(error, CostLimitExceeded)
                else "SYNC_TIMEOUT" if isinstance(error, TimeoutError) else "SYNC_FAILED")
        # SQL allowlists codes; raw provider/broker errors never enter storage/logs.
        await repository.fail_collection(job, code)
        logger.warning("Comparison collection attempt ended")
        return await repository.read_collection(user_id=job.user_id, case_id=job.case_id, collection_id=job.id)
    # A persistence timeout has an uncertain outcome. Leave the lease unchanged:
    # recover the committed result, or expire the task; never refetch automatically.
    return await repository.finish_collection(job, collection)
