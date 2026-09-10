from datetime import datetime, timezone
from uuid import UUID

import fakeredis.aioredis
import httpx
import pytest

from app.jobs_v22.cost_models import CostCountersV1, CostSummaryRecord, ALLOWED_COUNTER_KEYS
from app.jobs_v22.cost_persistence import CostSummaryOutbox, CostSummaryPersister


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")
CASE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)


def summary(*, revision: int = 3) -> CostSummaryRecord:
    counters = {key: 0 for key in ALLOWED_COUNTER_KEYS}
    counters.update(
        {
            "cost_schema_version": 1,
            "cost_ledger_revision": revision,
            "pricing_revision": 1,
        }
    )
    return CostSummaryRecord(
        job_id=JOB_ID,
        case_id=CASE_ID,
        job_kind="competitor_discovery",
        status="succeeded",
        attempt_count=1,
        ledger_revision=revision,
        cost_counters=CostCountersV1(counters),
        started_at=NOW,
        completed_at=NOW,
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_persister_sends_only_bounded_summary_fields() -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(200, json={"job_id": str(JOB_ID)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        persister = CostSummaryPersister(
            url="https://project.supabase.co",
            service_role_key="service-secret",
            http_client=client,
        )
        assert await persister.persist(summary()) is True

    body = captured["body"].decode("utf-8")
    assert captured["url"].endswith("/rest/v1/rpc/upsert_v22_job_cost_summary")
    assert "service-secret" not in body
    assert "customer" not in body
    assert '"p_ledger_revision":3' in body


@pytest.mark.anyio
async def test_persister_returns_false_without_exposing_provider_body() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="sensitive-provider-detail")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        persister = CostSummaryPersister(
            url="https://project.supabase.co",
            service_role_key="service-secret",
            http_client=client,
        )
        assert await persister.persist(summary()) is False


@pytest.mark.anyio
async def test_outbox_retries_only_summary_persistence() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503 if attempts == 1 else 200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outbox = CostSummaryOutbox(
            redis,
            prefix="test:v22",
            ttl_seconds=3600,
            persister=CostSummaryPersister(
                url="https://project.supabase.co",
                service_role_key="service-secret",
                http_client=client,
            ),
        )
        await outbox.enqueue(summary())
        assert await outbox.flush() == 0
        assert await outbox.flush() == 1
        assert await outbox.flush() == 0

    assert attempts == 2


def test_summary_revision_must_match_flat_counters() -> None:
    value = summary()

    with pytest.raises(ValueError, match="revision must match"):
        CostSummaryRecord.model_validate(
            {**value.model_dump(mode="python"), "ledger_revision": 4}
        )
