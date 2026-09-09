from uuid import UUID

import fakeredis.aioredis
import pytest

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.execution_plan_stage import (
    CheckpointedExecutionPlanStage,
    ExecutionPlanCheckpointError,
)
from app.report_v22.execution_plan import build_execution_plan
from app.report_v22.execution_plan_errors import ExecutionPlanError
from execution_plan_helpers import execution_plan_request


JOB_ID = UUID("50000000-0000-4000-8000-000000000174")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def checkpoints():
    return JobCheckpoints(
        fakeredis.aioredis.FakeRedis(decode_responses=False),
        prefix="test:execution-plan", ttl_seconds=3600,
    )


class CountingBuilder:
    def __init__(self):
        self.calls = 0

    def __call__(self, value):
        self.calls += 1
        return build_execution_plan(value)


@pytest.mark.anyio
async def test_stage_reuses_result_only_checkpoint() -> None:
    store, builder = checkpoints(), CountingBuilder()
    stage = CheckpointedExecutionPlanStage(builder=builder)
    value = execution_plan_request()
    first = await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    second = await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    assert first == second
    assert builder.calls == 1
    raw = await store.get(JOB_ID, stage.checkpoint_key(value))
    encoded = canonical_json_bytes(raw).decode()
    assert raw["schema_version"] == "execution_plan_checkpoint_v1"
    assert "verified_reprioritization_input" not in raw
    assert "version_diff_input" not in raw
    assert "raw_payload" not in encoded


@pytest.mark.anyio
async def test_stage_rejects_corrupt_checkpoint() -> None:
    store = checkpoints()
    stage = CheckpointedExecutionPlanStage()
    value = execution_plan_request()
    await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    key = stage.checkpoint_key(value)
    raw = await store.get(JOB_ID, key)
    raw["result_checksum"] = f"sha256:{'0' * 64}"
    await store.redis.set(
        store.keys.checkpoint(JOB_ID, key), canonical_json_bytes(raw), ex=store.ttl_seconds,
    )
    with pytest.raises(ExecutionPlanCheckpointError):
        await stage.build(job_id=JOB_ID, request=value, checkpoints=store)


def test_stage_rejects_mutation_before_checkpoint_lookup() -> None:
    stage = CheckpointedExecutionPlanStage()
    value = execution_plan_request()
    value.verified_reprioritization_input.first_party_input.snapshots[0].health_reasons.append(
        "mutated-after-checksum"
    )
    with pytest.raises(ExecutionPlanError) as exc:
        stage.checkpoint_key(value)
    assert exc.value.error_code == "V22_EXECUTION_PLAN_CHECKSUM_MISMATCH"
