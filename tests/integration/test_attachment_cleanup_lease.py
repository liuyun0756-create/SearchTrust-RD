from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.jobs_v22.attachment_cleanup_worker import (
    FINISH_SCRIPT, cleanup_keys, cleanup_v22_implementation_attachments,
)

pytestmark = [pytest.mark.redis_integration, pytest.mark.anyio]


async def test_concurrent_workers_execute_once_and_store_receipt(redis_client, redis_prefix):
    entered, release = asyncio.Event(), asyncio.Event()
    async def clean():
        entered.set()
        await release.wait()
        return {"processed": 0, "removed": 0, "failed": 0}
    cleaner = SimpleNamespace(run=AsyncMock(side_effect=clean))
    ctx = {"redis": redis_client, "attachment_cleanup": cleaner, "attachment_cleanup_prefix": redis_prefix}
    task = asyncio.create_task(cleanup_v22_implementation_attachments(ctx))
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.gather(*(cleanup_v22_implementation_attachments(ctx) for _ in range(10)))
    assert cleaner.run.await_count == 1
    lease, last_run = cleanup_keys(redis_prefix)
    assert 100 <= await redis_client.ttl(lease) <= 150
    release.set()
    await task
    receipt = json.loads(await redis_client.get(last_run))
    assert receipt["status"] == "ok" and receipt["processed"] == 0
    assert await redis_client.get(lease) is None
    assert 604_000 < await redis_client.ttl(last_run) <= 604_800


async def test_stale_owner_cannot_release_or_overwrite_new_owner(redis_client, redis_prefix):
    lease, last_run = cleanup_keys(redis_prefix)
    await redis_client.set(lease, "new-owner", ex=150)
    await redis_client.set(last_run, "previous-receipt", ex=600)
    assert await redis_client.eval(FINISH_SCRIPT, 2, lease, last_run, "stale-owner", "bad", 600) == 0
    assert await redis_client.get(lease) == b"new-owner"
    assert await redis_client.get(last_run) == b"previous-receipt"


async def test_expired_lease_allows_recovery_without_stale_write(redis_client, redis_prefix):
    lease, last_run = cleanup_keys(redis_prefix)
    await redis_client.set(lease, "old-owner", px=1)
    await asyncio.sleep(0.02)
    assert await redis_client.set(lease, "replacement", nx=True, ex=150)
    assert await redis_client.eval(FINISH_SCRIPT, 2, lease, last_run, "old-owner", "bad", 600) == 0
    assert await redis_client.eval(FINISH_SCRIPT, 2, lease, last_run, "replacement", "good", 600) == 1
    assert await redis_client.get(last_run) == b"good"


async def test_failed_batch_releases_lease_and_next_run_retries(redis_client, redis_prefix):
    cleaner = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("sensitive provider body")))
    ctx = {"redis": redis_client, "attachment_cleanup": cleaner, "attachment_cleanup_prefix": redis_prefix}
    await cleanup_v22_implementation_attachments(ctx)
    lease, last_run = cleanup_keys(redis_prefix)
    assert await redis_client.get(lease) is None
    raw = await redis_client.get(last_run)
    assert json.loads(raw)["status"] == "failed" and b"sensitive" not in raw
    cleaner.run = AsyncMock(return_value={"processed": 0, "removed": 0, "failed": 0})
    await cleanup_v22_implementation_attachments(ctx)
    assert json.loads(await redis_client.get(last_run))["status"] == "ok"
