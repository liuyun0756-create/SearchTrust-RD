import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.jobs_v22.capability_reconciler import CapabilityOrphanReconciler, reconcile_v22_capability_orphans
from app.jobs_v22.errors import TransientJobError


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_expiry_is_database_timed_bounded_and_registered_with_worker():
    from app.jobs_v22.worker import WorkerSettings
    assert any(job.name=="reconcile_v22_capability_orphans" for job in WorkerSettings.cron_jobs)
    job_id=uuid4()
    def respond(request):
        assert request.url.path.endswith("/expire_v22_prepared_capability_jobs")
        assert json.loads(request.content)=={"p_limit":100}
        return httpx.Response(200,json=[{"job_id":str(job_id)}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        reconciler=CapabilityOrphanReconciler(url="https://storage.example",service_role_key="test",http_client=client)
        assert await reconciler.expire()==[job_id]


@pytest.mark.anyio
@pytest.mark.parametrize("payload",[{},[{"job_id":"invalid"}],[{"job_id":str(uuid4()),"private":"data"}],[{"job_id":"00000000-0000-0000-0000-000000000001"}]*2])
async def test_malformed_expiry_receipt_is_not_treated_as_success(payload):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=payload))) as client:
        reconciler=CapabilityOrphanReconciler(url="https://storage.example",service_role_key="test",http_client=client)
        with pytest.raises(TransientJobError):
            await reconciler.expire()


@pytest.mark.anyio
async def test_cron_has_no_effect_when_disabled_and_defers_unknown_sql_outcome(caplog):
    await reconcile_v22_capability_orphans({})
    reconciler=AsyncMock()
    reconciler.expire.side_effect=RuntimeError("private sql details")
    await reconcile_v22_capability_orphans({"capability_orphan_reconciler":reconciler})
    assert "private sql details" not in caplog.text
    reconciler.expire.assert_awaited_once()
