from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from app.jobs_v22.verified_reconciler import (
    SupabaseVerifiedOrphanReconciler,
    reconcile_v22_verified_orphans,
)
from app.jobs_v22.worker import WorkerSettings


NOW = datetime(2026, 9, 14, 8, 30, tzinfo=timezone.utc)
JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
SECRET = "sensitive-service-or-case-payload"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_reconciler_calls_bounded_service_role_expiry_rpc() -> None:
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=[{"job_id": str(JOB_ID)}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reconciler = SupabaseVerifiedOrphanReconciler(
            url="https://storage.example",
            service_role_key="service-secret",
            http_client=client,
        )
        assert await reconciler.expire(now=NOW) == [JOB_ID]

    sent = calls[0]
    assert sent.url.path == "/rest/v1/rpc/expire_v22_stale_verified_jobs"
    assert sent.headers["authorization"] == "Bearer service-secret"
    assert sent.headers["apikey"] == "service-secret"
    assert sent.headers["accept-encoding"] == "identity"
    assert json.loads(sent.content) == {"p_now": NOW.isoformat(), "p_limit": 100}


@pytest.mark.anyio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_reconciler_defers_rate_limit_and_storage_outage_safely(
    status: int, caplog
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=SECRET)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reconciler = SupabaseVerifiedOrphanReconciler(
            url="https://storage.example",
            service_role_key="service-secret",
            http_client=client,
        )
        assert await reconciler.expire(now=NOW) == []

    assert SECRET not in caplog.text


@pytest.mark.anyio
async def test_reconciler_timeout_is_deferred_without_payload_logging(caplog) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(SECRET, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reconciler = SupabaseVerifiedOrphanReconciler(
            url="https://storage.example",
            service_role_key="service-secret",
            http_client=client,
        )
        assert await reconciler.expire(now=NOW) == []

    assert SECRET not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {"job_id": str(JOB_ID)},
        [{"job_id": str(JOB_ID), "payload": SECRET}],
        [{"job_id": "not-a-uuid"}],
        [{"job_id": str(JOB_ID)}] * 101,
    ],
)
async def test_reconciler_rejects_unbounded_or_malformed_ack_safely(body, caplog) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        reconciler = SupabaseVerifiedOrphanReconciler(
            url="https://storage.example",
            service_role_key="service-secret",
            http_client=client,
        )
        assert await reconciler.expire(now=NOW) == []

    assert SECRET not in caplog.text


@pytest.mark.anyio
async def test_cron_wrapper_is_noop_without_configured_reconciler() -> None:
    await reconcile_v22_verified_orphans({})


def test_verified_orphan_reconciler_is_registered_every_minute() -> None:
    jobs = {job.name: job for job in WorkerSettings.cron_jobs}
    configured = jobs["reconcile_v22_verified_orphans"]

    assert configured.coroutine is reconcile_v22_verified_orphans
    assert configured.minute == set(range(60))
    assert configured.max_tries == 1
