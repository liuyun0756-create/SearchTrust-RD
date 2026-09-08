from uuid import UUID

import fakeredis.aioredis
import pytest

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.first_party_findings_stage import (
    CheckpointedFirstPartyFindingsStage,
    FirstPartyFindingsCheckpointError,
)
from app.jobs_v22 import first_party_findings_stage
from app.report_v22.first_party_findings import build_first_party_findings
from test_v22_first_party_findings import ga4_value, gsc_value, request, trusted


JOB_ID = UUID("50000000-0000-4000-8000-000000000001")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def checkpoints():
    return JobCheckpoints(fakeredis.aioredis.FakeRedis(decode_responses=False), prefix="test:first-party", ttl_seconds=3600)


class CountingBuilder:
    def __init__(self):
        self.calls = 0

    def __call__(self, value):
        self.calls += 1
        return build_first_party_findings(value)


@pytest.mark.anyio
async def test_stage_reuses_validated_checkpoint_without_storing_source_content() -> None:
    store = checkpoints()
    builder = CountingBuilder()
    stage = CheckpointedFirstPartyFindingsStage(builder=builder)
    value = request(trusted("gsc", gsc_value(), 60), trusted("ga4", ga4_value(), 61))
    first = await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    second = await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    assert first == second
    assert builder.calls == 1
    raw = await store.get(JOB_ID, stage.checkpoint_key(value))
    assert raw["schema_version"] == "first_party_findings_checkpoint_v1"
    assert "snapshots" not in raw
    assert "raw_payload" not in canonical_json_bytes(raw).decode()


@pytest.mark.anyio
async def test_stage_rejects_corrupt_checkpoint() -> None:
    store = checkpoints()
    stage = CheckpointedFirstPartyFindingsStage()
    value = request(trusted("gsc", gsc_value(), 70), trusted("ga4", ga4_value(), 71))
    await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    key = stage.checkpoint_key(value)
    raw = await store.get(JOB_ID, key)
    raw["result_checksum"] = f"sha256:{'0' * 64}"
    await store.redis.set(store.keys.checkpoint(JOB_ID, key), canonical_json_bytes(raw), ex=store.ttl_seconds)
    with pytest.raises(FirstPartyFindingsCheckpointError):
        await stage.build(job_id=JOB_ID, request=value, checkpoints=store)


@pytest.mark.anyio
async def test_stage_version_change_uses_a_new_checkpoint(monkeypatch) -> None:
    store = checkpoints()
    builder = CountingBuilder()
    stage = CheckpointedFirstPartyFindingsStage(builder=builder)
    value = request(trusted("gsc", gsc_value(), 72), trusted("ga4", ga4_value(), 73))
    await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    monkeypatch.setattr(first_party_findings_stage, "STAGE_VERSION", "v22_first_party_findings_stage_v2")
    await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    assert builder.calls == 2
