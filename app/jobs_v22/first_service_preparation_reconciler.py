"""Recover SQL-registered collection work without a second charge or budget.

SQL claim owns eligibility, lease, attempt limit and deadline. Queue recovery
uses a minute-scoped ID so a retained ARQ failure cannot strand the SQL task.
Already frozen input takes the existing idempotent dispatch path, not collection.
"""
import logging
from uuid import UUID

from app.jobs_v22.models import utc_now

logger = logging.getLogger(__name__)


async def reconcile_v22_first_service_preparation(ctx):
    if not ctx.get("first_service_preparation_recovery_enabled"):
        return
    preparer = ctx.get("capability_first_service_preparer")
    if preparer is None or ctx.get("capability_submit_dispatch") is None:
        return
    rpc = preparer.repository.rpc
    try:
        rows = await rpc.post("list_v22_first_service_preparation_work", {
            "p_limit": 25, "p_max_attempts": ctx["max_attempts"],
        })
        if not isinstance(rows, list) or len(rows) > 25:
            raise ValueError("Invalid preparation list")
        work = []
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"job_id", "run_generation"}
                    or not isinstance(row["job_id"], str)
                    or type(row["run_generation"]) is not int or row["run_generation"] < 1):
                raise ValueError("Invalid preparation identity")
            work.append((UUID(row["job_id"]), row["run_generation"]))
        if len({job_id for job_id, _ in work}) != len(work):
            raise ValueError("Duplicate preparation identity")
    except Exception as exc:
        logger.warning("first-service collection polling deferred error=%s", type(exc).__name__)
        return
    bucket = int(ctx.get("clock", utc_now)().timestamp()) // 60
    for job_id, generation in work:
        try:
            await ctx["redis"].enqueue_job("prepare_v22_first_service", str(job_id), generation,
                _queue_name=ctx["first_service_preparation_queue_name"],
                _job_id=f"first-service-prepare:{job_id}:{generation}:recovery:{bucket}")
        except Exception as exc:
            logger.warning("first-service collection publication deferred error=%s", type(exc).__name__)
