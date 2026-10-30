"""Minute maintenance with an owner-fenced Redis lease and sanitized receipts."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from app.jobs_v22.attachment_cleanup import AttachmentCleanup

logger = logging.getLogger(__name__)
LEASE_SECONDS = 150
RUN_SECONDS = 90
RESULT_TTL_SECONDS = 604_800
FINISH_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
redis.call('DEL', KEYS[1])
return 1
"""


def cleanup_keys(prefix: str) -> tuple[str, str]:
    base = f"{prefix}:maintenance:implementation-attachments"
    return f"{base}:lease", f"{base}:last-run"


async def start_attachment_cleanup(ctx: dict, configured) -> None:
    if not configured.V22_IMPLEMENTATION_ATTACHMENT_CLEANUP_ENABLED:
        return
    client = httpx.AsyncClient(timeout=5, follow_redirects=False,
                              limits=httpx.Limits(max_connections=4, max_keepalive_connections=4))
    try:
        cleaner = AttachmentCleanup(url=configured.V22_SUPABASE_URL,
            service_role_key=configured.V22_SUPABASE_SERVICE_ROLE_KEY.get_secret_value(),
            http_client=client)
    except Exception:
        await client.aclose()
        logger.warning("implementation attachment cleanup status=unavailable reason=configuration")
        return
    ctx["attachment_cleanup_client"] = client
    ctx["attachment_cleanup"] = cleaner
    ctx["attachment_cleanup_prefix"] = configured.V22_REDIS_PREFIX
    logger.info("implementation attachment cleanup enabled interval_seconds=60 batch_limit=20")


async def stop_attachment_cleanup(ctx: dict) -> None:
    client = ctx.get("attachment_cleanup_client")
    if client is not None:
        await client.aclose()


async def cleanup_v22_implementation_attachments(ctx: dict) -> None:
    cleaner = ctx.get("attachment_cleanup")
    if cleaner is None:
        return
    redis = ctx["redis"]
    lease, last_run = cleanup_keys(ctx["attachment_cleanup_prefix"])
    owner = str(uuid4())
    try:
        async with asyncio.timeout(5):
            acquired = await redis.set(lease, owner, nx=True, ex=LEASE_SECONDS)
    except Exception:
        logger.warning("implementation attachment cleanup status=unavailable reason=lease")
        return
    if not acquired:
        logger.info("implementation attachment cleanup status=busy")
        return
    receipt = {"run_id": owner, "started_at": datetime.now(timezone.utc).isoformat(),
               "status": "failed", "processed": None, "removed": None, "failed": None}
    logger.info("implementation attachment cleanup started run_id=%s", owner)
    try:
        async with asyncio.timeout(RUN_SECONDS):
            result = await cleaner.run()
        receipt.update(result)
        receipt["status"] = "partial" if result["failed"] else "ok"
    except TimeoutError:
        receipt["status"] = "timeout"
    except asyncio.CancelledError:
        receipt["status"] = "cancelled"
        raise
    except Exception:
        receipt["status"] = "failed"
    finally:
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        try:
            async with asyncio.timeout(5):
                recorded = await redis.eval(FINISH_SCRIPT, 2, lease, last_run,
                    owner, json.dumps(receipt, separators=(",", ":")), RESULT_TTL_SECONDS)
            if recorded != 1:
                logger.warning("implementation attachment cleanup status=lease_lost run_id=%s", owner)
        except Exception:
            logger.warning("implementation attachment cleanup status=receipt_unavailable run_id=%s", owner)
        logger.info("implementation attachment cleanup result=%s", json.dumps(receipt, separators=(",", ":")))
