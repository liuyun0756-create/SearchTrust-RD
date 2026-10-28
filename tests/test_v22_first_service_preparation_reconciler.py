from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.jobs_v22.first_service_preparation_reconciler import reconcile_v22_first_service_preparation as reconcile


@pytest.fixture
def anyio_backend():
    return "asyncio"


def context(rows):
    rpc = SimpleNamespace(post=AsyncMock(return_value=rows))
    return dict(first_service_preparation_recovery_enabled=True,
        capability_first_service_preparer=SimpleNamespace(repository=SimpleNamespace(rpc=rpc)),
        capability_submit_dispatch=AsyncMock(), redis=SimpleNamespace(enqueue_job=AsyncMock()),
        first_service_preparation_queue_name="test-first-service", max_attempts=3,
        clock=lambda: datetime(2026, 10, 21, tzinfo=timezone.utc))


@pytest.mark.anyio
async def test_disabled_or_incomplete_runtime_does_not_read_or_enqueue():
    await reconcile({})
    for key in ["first_service_preparation_recovery_enabled", "capability_first_service_preparer", "capability_submit_dispatch"]:
        ctx = context([])
        rpc = ctx["capability_first_service_preparer"].repository.rpc
        ctx.pop(key)
        await reconcile(ctx)
        rpc.post.assert_not_awaited()
        ctx["redis"].enqueue_job.assert_not_awaited()


@pytest.mark.anyio
async def test_original_identity_generation_queue_and_bounded_retry_are_preserved():
    from app.jobs_v22.worker import WorkerSettings
    assert any(j.name == "reconcile_v22_first_service_preparation" for j in WorkerSettings.cron_jobs)
    job = str(uuid4())
    ctx = context([dict(job_id=job, run_generation=2)])
    await reconcile(ctx)
    ctx["capability_first_service_preparer"].repository.rpc.post.assert_awaited_once_with(
        "list_v22_first_service_preparation_work", {"p_limit": 25, "p_max_attempts": 3})
    first = ctx["redis"].enqueue_job.call_args
    assert first.args == ("prepare_v22_first_service", job, 2)
    assert first.kwargs["_queue_name"] == "test-first-service"
    await reconcile(ctx)
    assert ctx["redis"].enqueue_job.call_args == first
    now = ctx["clock"]()
    ctx["clock"] = lambda: now + timedelta(minutes=1)
    await reconcile(ctx)
    assert ctx["redis"].enqueue_job.call_args.kwargs["_job_id"] != first.kwargs["_job_id"]
    ctx["capability_submit_dispatch"].assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("rows", [{}, [dict(job_id="bad", run_generation=1)],
    [dict(job_id=str(uuid4()), run_generation=True)], [dict(job_id=str(uuid4()), run_generation=0)],
    [dict(job_id=str(uuid4()), run_generation=1, secret="bad")],
    [dict(job_id="00000000-0000-0000-0000-000000000001", run_generation=1)] * 2])
async def test_malformed_list_is_rejected_before_publication(rows):
    ctx = context(rows)
    await reconcile(ctx)
    ctx["redis"].enqueue_job.assert_not_awaited()


@pytest.mark.anyio
async def test_publication_failure_does_not_block_later_tasks_or_expose_errors(caplog):
    ctx = context([dict(job_id=str(uuid4()), run_generation=1) for _ in range(2)])
    ctx["redis"].enqueue_job.side_effect = [RuntimeError("private data"), None]
    await reconcile(ctx)
    assert ctx["redis"].enqueue_job.await_count == 2
    assert "private data" not in caplog.text
