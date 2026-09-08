from uuid import UUID

import fakeredis.aioredis
import pytest

from app.jobs_v22 import cross_source_findings_stage
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.cross_source_findings_stage import (
    CheckpointedCrossSourceFindingsStage, CrossSourceFindingsCheckpointError,
)
from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.cross_source_findings import build_cross_source_findings
from test_v22_cross_source_findings import cross_request


JOB_ID = UUID("50000000-0000-4000-8000-000000000071")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def checkpoints():
    return JobCheckpoints(fakeredis.aioredis.FakeRedis(decode_responses=False),
        prefix="test:cross-source", ttl_seconds=3600)


class CountingBuilder:
    def __init__(self):
        self.calls = 0

    def __call__(self, value):
        self.calls += 1
        return build_cross_source_findings(value)


@pytest.mark.anyio
async def test_stage_reuses_checkpoint_and_stores_no_trusted_input() -> None:
    store, builder = checkpoints(), CountingBuilder()
    stage = CheckpointedCrossSourceFindingsStage(builder=builder)
    value = cross_request()
    first = await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    second = await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    assert first == second
    assert builder.calls == 1
    raw = await store.get(JOB_ID, stage.checkpoint_key(value))
    encoded = canonical_json_bytes(raw).decode()
    assert raw["schema_version"] == "cross_source_findings_checkpoint_v1"
    assert "first_party_input" not in raw
    assert "first_party_result" not in raw
    assert "raw_payload" not in encoded


@pytest.mark.anyio
async def test_stage_rejects_corrupt_checkpoint() -> None:
    store = checkpoints()
    stage = CheckpointedCrossSourceFindingsStage()
    value = cross_request()
    await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    key = stage.checkpoint_key(value)
    raw = await store.get(JOB_ID, key)
    raw["result_checksum"] = f"sha256:{'0' * 64}"
    await store.redis.set(store.keys.checkpoint(JOB_ID, key), canonical_json_bytes(raw), ex=store.ttl_seconds)
    with pytest.raises(CrossSourceFindingsCheckpointError):
        await stage.build(job_id=JOB_ID, request=value, checkpoints=store)


@pytest.mark.anyio
async def test_stage_version_change_uses_new_checkpoint(monkeypatch) -> None:
    store, builder = checkpoints(), CountingBuilder()
    stage = CheckpointedCrossSourceFindingsStage(builder=builder)
    value = cross_request()
    await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    monkeypatch.setattr(cross_source_findings_stage, "STAGE_VERSION", "v22_cross_source_findings_stage_v2")
    await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    assert builder.calls == 2
