from uuid import UUID

import fakeredis.aioredis
import pytest

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.version_diff_errors import VersionDiffError
from app.jobs_v22.version_diff_stage import (
    CheckpointedVersionDiffStage,
    VersionDiffCheckpointError,
)
from app.report_v22.version_diff import build_version_diff
from version_diff_helpers import version_diff_request


JOB_ID = UUID("50000000-0000-4000-8000-000000000073")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def checkpoints():
    return JobCheckpoints(
        fakeredis.aioredis.FakeRedis(decode_responses=False),
        prefix="test:version-diff",
        ttl_seconds=3600,
    )


class CountingBuilder:
    def __init__(self):
        self.calls = 0

    def __call__(self, value):
        self.calls += 1
        return build_version_diff(value)


@pytest.mark.anyio
async def test_stage_reuses_result_only_checkpoint() -> None:
    store, builder = checkpoints(), CountingBuilder()
    stage = CheckpointedVersionDiffStage(builder=builder)
    value = version_diff_request()
    first = await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    second = await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    assert first == second
    assert builder.calls == 1
    raw = await store.get(JOB_ID, stage.checkpoint_key(value))
    encoded = canonical_json_bytes(raw).decode()
    assert raw["schema_version"] == "version_diff_checkpoint_v1"
    assert "parent_report" not in raw
    assert "verified_reprioritization_input" not in raw
    assert "raw_payload" not in encoded


@pytest.mark.anyio
async def test_stage_rejects_corrupt_checkpoint() -> None:
    store = checkpoints()
    stage = CheckpointedVersionDiffStage()
    value = version_diff_request()
    await stage.build(job_id=JOB_ID, request=value, checkpoints=store)
    key = stage.checkpoint_key(value)
    raw = await store.get(JOB_ID, key)
    raw["result_checksum"] = f"sha256:{'0' * 64}"
    await store.redis.set(
        store.keys.checkpoint(JOB_ID, key), canonical_json_bytes(raw), ex=store.ttl_seconds,
    )
    with pytest.raises(VersionDiffCheckpointError):
        await stage.build(job_id=JOB_ID, request=value, checkpoints=store)


def test_stage_rejects_mutated_input_before_checkpoint_lookup() -> None:
    stage = CheckpointedVersionDiffStage()
    value = version_diff_request()
    value.parent_report.findings[0].statement = "Changed after the checksum was created."
    with pytest.raises(VersionDiffError) as exc:
        stage.checkpoint_key(value)
    assert exc.value.error_code == "V22_VERSION_DIFF_CHECKSUM_MISMATCH"
