from uuid import UUID

import fakeredis.aioredis
import pytest

from app.jobs_v22.capability_public_stage import CheckpointedCapabilityPublicStage
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError
from test_v22_capability_public import inputs


@pytest.fixture
def anyio_backend(): return "asyncio"


@pytest.mark.anyio
async def test_replays_same_frozen_diagnosis_without_a_new_generation_or_model_call(monkeypatch):
    import app.jobs_v22.capability_public_stage as module
    manifest, request = inputs([{"status_code": 503}])
    checkpoints = JobCheckpoints(fakeredis.aioredis.FakeRedis(decode_responses=False), prefix="capability-test", ttl_seconds=600)
    stage = CheckpointedCapabilityPublicStage()
    first = await stage.build(job_id=manifest.job_id, manifest=manifest, request=request, checkpoints=checkpoints)
    monkeypatch.setattr(module, "build_public_capability_report", lambda *args: pytest.fail("A cached report must not be regenerated"))
    assert await stage.build(job_id=manifest.job_id, manifest=manifest, request=request, checkpoints=checkpoints) == first
    await checkpoints.redis.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("corruption", ["job_id", "input_digest", "result_checksum", "result", "extra", "json"])
async def test_corrupt_checkpoint_is_not_delivered_as_success(corruption):
    manifest, request = inputs([{"status_code": 503}])
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints = JobCheckpoints(redis, prefix="capability-test", ttl_seconds=600)
    stage = CheckpointedCapabilityPublicStage()
    await stage.build(job_id=manifest.job_id, manifest=manifest, request=request, checkpoints=checkpoints)
    keys = [key async for key in redis.scan_iter(match="*:checkpoint:*")]
    assert len(keys) == 1
    import json
    raw = json.loads(await redis.get(keys[0]))
    raw[corruption] = "invalid"
    await redis.set(keys[0], b"not-json" if corruption == "json" else canonical_json_bytes(raw))
    with pytest.raises(DeterministicJobError) as error:
        await stage.build(job_id=manifest.job_id, manifest=manifest, request=request, checkpoints=checkpoints)
    assert error.value.error_code == "V22_CAPABILITY_PUBLIC_CHECKPOINT_INVALID"
    await redis.aclose()


@pytest.mark.anyio
async def test_stage_rejects_the_wrong_task_and_frozen_request_drift():
    manifest, request = inputs([{"status_code": 503}])
    checkpoints = JobCheckpoints(fakeredis.aioredis.FakeRedis(decode_responses=False), prefix="capability-test", ttl_seconds=600)
    stage = CheckpointedCapabilityPublicStage()
    with pytest.raises(DeterministicJobError) as error:
        await stage.build(job_id=UUID(int=999), manifest=manifest, request=request, checkpoints=checkpoints)
    assert error.value.error_code == "V22_CAPABILITY_PUBLIC_INPUT_INVALID"
    request.evidence_input.context.primary_service = "Another service"
    with pytest.raises(DeterministicJobError) as error:
        await stage.build(job_id=manifest.job_id, manifest=manifest, request=request, checkpoints=checkpoints)
    assert error.value.error_code == "V22_CAPABILITY_PUBLIC_RESULT_INVALID"
    await checkpoints.redis.aclose()
