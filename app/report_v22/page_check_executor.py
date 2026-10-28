"""Single-attempt internal execution. Cancellation/uncertain saves never refetch."""
import asyncio

from app.collectors.page_implementation import collect_page_implementation


async def execute_page_check(repository, check_id, *, resolver=None, client_factory=None):
    # A lost claim acknowledgement must not be interpreted as permission to retry.
    job = await repository.claim_check(check_id)
    if job is None:
        return None
    try:
        observation = await collect_page_implementation(job.plan, observation_id=job.id,
            resolver=resolver, client_factory=client_factory)
    except asyncio.CancelledError:
        raise
    except Exception:
        # No exception strings, partial page material or inferred negative results.
        await repository.fail_check(job)
        return await repository.read_check(user_id=job.user_id, case_id=job.case_id, check_id=job.id)
    # Keep a complete observation for a same-save retry if the caller retains it;
    # execution itself never retries either the fetch or this persistence operation.
    return await repository.finish_check(job, observation)
