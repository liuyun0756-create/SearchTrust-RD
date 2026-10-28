"""Dispatch explicit saved page checks only. Expiry never implies a fresh visit."""
import json
import logging
from uuid import UUID

from app.report_v22.page_check_executor import execute_page_check

logger = logging.getLogger(__name__)


async def reconcile_v22_page_checks(ctx):
    repository = ctx.get("page_check_repository")
    if repository is None:
        return
    try:
        ids = json.loads(await repository._rpc("prepare_v22_page_check_dispatch", {}))
        if not isinstance(ids, list) or len(ids) > 20 or len(set(ids)) != len(ids):
            raise ValueError("invalid pending list")
        ids = [str(UUID(i)) for i in ids]
        for job_id in ids:
            await ctx["redis"].enqueue_job("execute_v22_page_check", job_id,
                _job_id=f"page-check:{job_id}", _queue_name=ctx["page_check_queue"])
    except Exception:
        logger.warning("Page check dispatch deferred")


async def execute_v22_page_check(ctx, job_id):
    repository = ctx.get("page_check_repository")
    if repository is None:
        return
    # Result bodies are stored only in SQL, not retained in Redis.
    await execute_page_check(repository, UUID(job_id))
