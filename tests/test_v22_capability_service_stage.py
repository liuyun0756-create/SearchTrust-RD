import json
from uuid import UUID

import fakeredis.aioredis
import pytest

from app.jobs_v22.capability_service_stage import CheckpointedCapabilityServiceStage
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError
from test_v22_capability_service import inputs


@pytest.fixture
def anyio_backend(): return "asyncio"


@pytest.mark.anyio
async def test_frozen_service_result_replays_without_regeneration_or_a_new_external_call(monkeypatch):
    import app.jobs_v22.capability_service_stage as module
    manifest,request,parent=inputs(mode="after_acquisition")
    redis=fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints=JobCheckpoints(redis,prefix="service-capability-test",ttl_seconds=600)
    stage=CheckpointedCapabilityServiceStage()
    kwargs=dict(job_id=manifest.job_id,manifest=manifest,request=request,previous_report=parent,checkpoints=checkpoints)
    first=await stage.build(**kwargs)
    monkeypatch.setattr(module,"build_service_capability_report",lambda *args,**kwargs:pytest.fail("Must not regenerate a saved diagnosis"))
    assert await stage.build(**kwargs)==first
    await redis.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("field",["job_id","input_digest","result_checksum","result","extra","json"])
async def test_corrupt_service_checkpoints_are_not_success(field):
    manifest,request,_=inputs()
    redis=fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints=JobCheckpoints(redis,prefix="service-capability-test",ttl_seconds=600)
    stage=CheckpointedCapabilityServiceStage()
    kwargs=dict(job_id=manifest.job_id,manifest=manifest,request=request,checkpoints=checkpoints)
    await stage.build(**kwargs)
    keys=[key async for key in redis.scan_iter(match="*:checkpoint:*")]
    assert len(keys)==1
    payload=json.loads(await redis.get(keys[0]));payload[field]="invalid"
    await redis.set(keys[0],b"not-json" if field=="json" else canonical_json_bytes(payload))
    with pytest.raises(DeterministicJobError) as error: await stage.build(**kwargs)
    assert error.value.error_code=="V22_CAPABILITY_SERVICE_CHECKPOINT_INVALID"
    await redis.aclose()


@pytest.mark.anyio
async def test_wrong_job_and_changed_previous_payload_are_not_accepted_as_replays():
    manifest,request,parent=inputs(mode="after_acquisition")
    redis=fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints=JobCheckpoints(redis,prefix="service-capability-test",ttl_seconds=600)
    stage=CheckpointedCapabilityServiceStage()
    with pytest.raises(DeterministicJobError) as error:
        await stage.build(job_id=UUID(int=111),manifest=manifest,request=request,previous_report=parent,checkpoints=checkpoints)
    assert error.value.error_code=="V22_CAPABILITY_SERVICE_INPUT_INVALID"
    kwargs=dict(job_id=manifest.job_id,manifest=manifest,request=request,checkpoints=checkpoints)
    await stage.build(**kwargs,previous_report=parent)
    parent["summary"]="Changed after the input was frozen."
    with pytest.raises(DeterministicJobError) as error: await stage.build(**kwargs,previous_report=parent)
    assert error.value.error_code=="V22_CAPABILITY_SERVICE_RESULT_INVALID"
    await redis.aclose()
