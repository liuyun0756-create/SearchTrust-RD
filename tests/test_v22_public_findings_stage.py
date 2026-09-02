from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from uuid import UUID

import fakeredis.aioredis
import pytest

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.public_findings_stage import (
    CheckpointedPublicFindingsStage,
    PublicFindingsCheckpointError,
    PublicFindingsResultError,
)
from app.report_v22.findings import build_public_findings
from app.report_v22.findings_errors import FindingsError
from app.report_v22.findings_models import PublicFindingsInput, PublicFindingsResult
from findings_helpers import collection, market, request, site


JOB_ID = UUID("55555555-5555-4555-8555-555555555555")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def findings_request() -> PublicFindingsInput:
    serp = market()
    return request(site(), serp, collection(serp))


def saved_checkpoints() -> JobCheckpoints:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    return JobCheckpoints(redis, prefix="test:v22", ttl_seconds=604800)


class CountingBuilder:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, value: PublicFindingsInput) -> PublicFindingsResult:
        self.calls += 1
        return build_public_findings(value)


class RaisingBuilder:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def __call__(self, value: PublicFindingsInput) -> PublicFindingsResult:
        raise self.error


@pytest.mark.anyio
async def test_stage_persists_and_reuses_a_validated_result() -> None:
    checkpoints = saved_checkpoints()
    builder = CountingBuilder()
    stage = CheckpointedPublicFindingsStage(builder=builder)
    value = findings_request()

    first = await stage.build(job_id=JOB_ID, request=value, checkpoints=checkpoints)
    repeated = await stage.build(job_id=JOB_ID, request=value, checkpoints=checkpoints)

    assert repeated == first
    assert builder.calls == 1
    raw = await checkpoints.get(JOB_ID, stage.checkpoint_key(value))
    assert raw is not None
    assert raw["schema_version"] == "public_findings_checkpoint_v1"
    assert raw["job_id"] == str(JOB_ID)
    assert raw["ruleset_version"] == "v22_public_findings_v1"
    assert raw["result_checksum"] == request_digest(raw["result"])


@pytest.mark.anyio
@pytest.mark.parametrize(
    "corruption",
    [
        "schema_version",
        "job_id",
        "input_digest",
        "ruleset_version",
        "result_checksum",
        "result_shape",
    ],
)
async def test_corrupt_or_misbound_checkpoint_is_rejected(corruption: str) -> None:
    checkpoints = saved_checkpoints()
    value = findings_request()
    stage = CheckpointedPublicFindingsStage()
    key = stage.checkpoint_key(value)
    await stage.build(job_id=JOB_ID, request=value, checkpoints=checkpoints)
    raw = await checkpoints.get(JOB_ID, key)
    assert raw is not None
    if corruption == "schema_version":
        raw["schema_version"] = "public_findings_checkpoint_v0"
    elif corruption == "job_id":
        raw["job_id"] = "66666666-6666-4666-8666-666666666666"
    elif corruption == "input_digest":
        raw["input_digest"] = f"sha256:{'0' * 64}"
    elif corruption == "ruleset_version":
        raw["ruleset_version"] = "v22_public_findings_v0"
    elif corruption == "result_checksum":
        raw["result_checksum"] = f"sha256:{'f' * 64}"
    else:
        raw["result"] = {"ruleset_version": "v22_public_findings_v1"}
    await checkpoints.redis.set(
        checkpoints.keys.checkpoint(JOB_ID, key),
        canonical_json_bytes(raw),
        ex=checkpoints.ttl_seconds,
    )

    with pytest.raises(PublicFindingsCheckpointError) as exc:
        await stage.build(job_id=JOB_ID, request=value, checkpoints=checkpoints)

    assert exc.value.error_code == "V22_PUBLIC_FINDINGS_CHECKPOINT_INVALID"
    assert exc.value.retryable is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kind",
    [
        "INPUT_INVALID",
        "REFERENCE_INVALID",
        "ID_CONFLICT",
        "LIMIT_EXCEEDED",
        "BINDING_INVALID",
        "CHECKSUM_MISMATCH",
    ],
)
async def test_known_findings_errors_keep_their_safe_classification(kind: str) -> None:
    error = FindingsError(kind)  # type: ignore[arg-type]
    stage = CheckpointedPublicFindingsStage(builder=RaisingBuilder(error))

    with pytest.raises(FindingsError) as exc:
        await stage.build(
            job_id=JOB_ID,
            request=findings_request(),
            checkpoints=saved_checkpoints(),
        )

    assert exc.value is error
    assert exc.value.error_code == f"V22_FINDINGS_{kind}"
    assert exc.value.retryable is False


@pytest.mark.anyio
async def test_unknown_builder_error_is_not_reclassified_or_hidden() -> None:
    error = RuntimeError("private implementation detail")
    stage = CheckpointedPublicFindingsStage(builder=RaisingBuilder(error))

    with pytest.raises(RuntimeError) as exc:
        await stage.build(
            job_id=JOB_ID,
            request=findings_request(),
            checkpoints=saved_checkpoints(),
        )

    assert exc.value is error


@pytest.mark.anyio
async def test_invalid_builder_result_is_a_safe_deterministic_failure() -> None:
    stage = CheckpointedPublicFindingsStage(
        builder=lambda _: {},  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(PublicFindingsResultError) as exc:
        await stage.build(
            job_id=JOB_ID,
            request=findings_request(),
            checkpoints=saved_checkpoints(),
        )

    assert exc.value.error_code == "V22_PUBLIC_FINDINGS_RESULT_INVALID"
    assert exc.value.retryable is False


@pytest.mark.anyio
async def test_result_larger_than_the_request_limit_is_not_checkpointed() -> None:
    checkpoints = saved_checkpoints()
    value = findings_request()
    result = build_public_findings(value)
    value.limits.max_bytes = 1
    stage = CheckpointedPublicFindingsStage(builder=lambda _: result)

    with pytest.raises(PublicFindingsResultError):
        await stage.build(job_id=JOB_ID, request=value, checkpoints=checkpoints)

    assert await checkpoints.get(JOB_ID, stage.checkpoint_key(value)) is None


@pytest.mark.anyio
async def test_invalid_request_is_rejected_before_the_builder_runs() -> None:
    builder = CountingBuilder()
    stage = CheckpointedPublicFindingsStage(builder=builder)
    raw = findings_request().model_dump(mode="json")
    raw["unexpected_private_value"] = "must not be echoed"

    with pytest.raises(FindingsError) as exc:
        await stage.build(job_id=JOB_ID, request=raw, checkpoints=saved_checkpoints())

    assert exc.value.error_code == "V22_FINDINGS_INPUT_INVALID"
    assert builder.calls == 0
    assert "must not be echoed" not in exc.value.user_message


@pytest.mark.anyio
async def test_concurrent_first_execution_returns_one_canonical_result() -> None:
    checkpoints = saved_checkpoints()
    builder = CountingBuilder()
    stage = CheckpointedPublicFindingsStage(builder=builder)
    value = findings_request()

    first, second = await asyncio.gather(
        stage.build(job_id=JOB_ID, request=value, checkpoints=checkpoints),
        stage.build(job_id=JOB_ID, request=value, checkpoints=checkpoints),
    )

    assert first == second
    assert 1 <= builder.calls <= 2
    assert await checkpoints.get(JOB_ID, stage.checkpoint_key(value)) is not None


@pytest.mark.anyio
async def test_logs_exclude_request_and_evidence_values(caplog: pytest.LogCaptureFixture) -> None:
    checkpoints = saved_checkpoints()
    stage = CheckpointedPublicFindingsStage()
    value = findings_request()
    sensitive = "SECRET-CUSTOMER-ADDRESS-AND-PHONE-555-0100"
    value.evidence_input.context.primary_service = sensitive
    caplog.set_level(logging.INFO, logger="app.jobs_v22.public_findings_stage")

    await stage.build(job_id=JOB_ID, request=value, checkpoints=checkpoints)
    await stage.build(job_id=JOB_ID, request=value, checkpoints=checkpoints)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert sensitive not in messages
    assert str(JOB_ID) in messages
    assert "checkpoint stored" in messages
    assert "checkpoint hit" in messages


@pytest.mark.anyio
async def test_changed_input_uses_a_distinct_checkpoint() -> None:
    checkpoints = saved_checkpoints()
    builder = CountingBuilder()
    stage = CheckpointedPublicFindingsStage(builder=builder)
    original = findings_request()
    changed = deepcopy(original)
    changed.evidence_input.context.primary_service = "Emergency plumbing"

    assert stage.checkpoint_key(original) != stage.checkpoint_key(changed)
    first = await stage.build(job_id=JOB_ID, request=original, checkpoints=checkpoints)
    second = await stage.build(job_id=JOB_ID, request=changed, checkpoints=checkpoints)

    assert builder.calls == 2
    assert first.site_rollup.site_url == second.site_rollup.site_url


@pytest.mark.anyio
async def test_equivalent_model_and_json_input_share_the_checkpoint() -> None:
    checkpoints = saved_checkpoints()
    builder = CountingBuilder()
    stage = CheckpointedPublicFindingsStage(builder=builder)
    value = findings_request()

    first = await stage.build(job_id=JOB_ID, request=value, checkpoints=checkpoints)
    repeated = await stage.build(
        job_id=JOB_ID,
        request=value.model_dump(mode="json"),
        checkpoints=checkpoints,
    )

    assert repeated == first
    assert builder.calls == 1
