from uuid import UUID

import fakeredis.aioredis
import pytest

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.digest import request_digest
from app.jobs_v22.verified_reprioritization_stage import (
    CheckpointedVerifiedReprioritizationStage,
    VerifiedReprioritizationCheckpointError,
)
from app.report_v22.verified_reprioritization import build_verified_reprioritization
from verified_reprioritization_helpers import verified_request


JOB_ID = UUID("50000000-0000-4000-8000-000000000072")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def checkpoints():
    return JobCheckpoints(
        fakeredis.aioredis.FakeRedis(decode_responses=False),
        prefix="test:verified-reprioritization",
        ttl_seconds=3600,
    )


class CountingBuilder:
    def __init__(self):
        self.calls = 0

    def __call__(self, value):
        self.calls += 1
        return build_verified_reprioritization(value)


@pytest.mark.anyio
async def test_stage_reuses_result_only_checkpoint() -> None:
    store, builder = checkpoints(), CountingBuilder()
    stage = CheckpointedVerifiedReprioritizationStage(builder=builder)
    value = verified_request()
    first = await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    second = await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    assert first == second
    assert builder.calls == 1
    raw = await store.get(JOB_ID, stage.checkpoint_key(value))
    encoded = canonical_json_bytes(raw).decode()
    assert raw["schema_version"] == "verified_reprioritization_checkpoint_v1"
    assert "first_party_input" not in raw
    assert "public_findings_input" not in raw
    assert "raw_payload" not in encoded


@pytest.mark.anyio
async def test_stage_rejects_corrupt_checkpoint() -> None:
    store = checkpoints()
    stage = CheckpointedVerifiedReprioritizationStage()
    value = verified_request()
    await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    key = stage.checkpoint_key(value)
    raw = await store.get(JOB_ID, key)
    raw["result_checksum"] = f"sha256:{'0' * 64}"
    await store.redis.set(
        store.keys.checkpoint(JOB_ID, key), canonical_json_bytes(raw), ex=store.ttl_seconds,
    )
    with pytest.raises(VerifiedReprioritizationCheckpointError):
        await stage.build(job_id=JOB_ID, request=value, checkpoints=store)


def test_stage_key_is_stable_when_first_party_snapshots_are_reordered() -> None:
    stage = CheckpointedVerifiedReprioritizationStage()
    value = verified_request()
    reordered_input = value.first_party_input.model_copy(update={
        "snapshots": list(reversed(value.first_party_input.snapshots)),
    })
    reordered = value.model_copy(update={
        "first_party_input": reordered_input,
        "first_party_input_checksum": request_digest(reordered_input),
    })
    assert stage.checkpoint_key(value) == stage.checkpoint_key(reordered)
