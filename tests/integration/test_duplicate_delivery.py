from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

import pytest

from app.jobs_v22.callbacks import CallbackSynchronizer
from app.jobs_v22.cost_persistence import CostSummaryOutbox
from app.jobs_v22.models import utc_now
from app.jobs_v22.store import DurableJobStore
from app.jobs_v22.worker import execute_v22_job
from app.report_v22.models import ReportV22


pytestmark = [pytest.mark.redis_integration, pytest.mark.anyio]

ROOT = Path(__file__).resolve().parents[2]
JOB_ID = UUID("55555555-5555-4555-8555-555555555552")
CASE_ID = UUID("11111111-1111-4111-8111-111111111111")


class RecordingExecutor:
    def __init__(self, report: ReportV22) -> None:
        self.report = report
        self.calls = 0

    async def execute(self, **_kwargs) -> ReportV22:
        self.calls += 1
        return self.report


class RecordingSender:
    def __init__(self) -> None:
        self.deliveries: list[tuple[int, str]] = []

    async def send(self, state) -> bool:
        self.deliveries.append((state.revision, state.status))
        return True


class RecordingPersister:
    def __init__(self) -> None:
        self.summaries = []

    async def persist(self, summary) -> bool:
        self.summaries.append(summary)
        return True


async def test_duplicate_physical_delivery_settles_terminal_side_effects_once(
    redis_client,
    redis_prefix: str,
    real_store: DurableJobStore,
) -> None:
    await real_store.register_job(
        job_id=JOB_ID,
        case_id=CASE_ID,
        idempotency_key="duplicate-physical-delivery",
        request_payload={"case_id": str(CASE_ID), "report_type": "prospect"},
        now=utc_now(),
    )
    report = ReportV22.model_validate_json(
        (ROOT / "contracts" / "v2.2" / "fixtures" / "prospect.json").read_text(
            encoding="utf-8"
        )
    )
    executor = RecordingExecutor(report)
    sender = RecordingSender()
    persister = RecordingPersister()
    outbox = CostSummaryOutbox(
        redis_client,
        prefix=redis_prefix,
        ttl_seconds=3600,
        persister=persister,  # type: ignore[arg-type]
    )
    synchronizer = CallbackSynchronizer(real_store, sender)
    context = {
        "redis": redis_client,
        "store": real_store,
        "executor": executor,
        "callback_synchronizer": synchronizer,
        "cost_summary_outbox": outbox,
        "state_ttl_seconds": 120,
        "max_attempts": 3,
        "heartbeat_seconds": 30,
        "lease_seconds": 60,
    }

    await asyncio.gather(
        execute_v22_job(context, str(JOB_ID), 1),
        execute_v22_job(context, str(JOB_ID), 1),
    )
    await execute_v22_job(context, str(JOB_ID), 1)
    await synchronizer.sync(JOB_ID)
    await synchronizer.sync(JOB_ID)

    state = await real_store.require_state(JOB_ID)
    assert state.status == "succeeded"
    assert state.report == report
    assert executor.calls == 1
    assert sender.deliveries.count((state.revision, "succeeded")) == 1
    assert state.callback_synced_revision == state.revision
    assert len(persister.summaries) == 1
    assert persister.summaries[0].ledger_revision == state.cost_counters["cost_ledger_revision"]
