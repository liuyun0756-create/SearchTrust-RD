"""End-to-end deterministic Verified generation over a frozen source graph."""

from datetime import timedelta

import fakeredis.aioredis
import pytest

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.jobs_v22.verified_report_pipeline import VerifiedReportPipeline
from verified_pipeline_helpers import JOB_ID, PARENT_ID, VERIFIED_AT, frozen_public_fixture


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_complete_pipeline_preserves_parent_and_bound_first_party_identities():
    from verified_pipeline_helpers import resolve_fixture

    resolved, request = await resolve_fixture()
    checkpoints = JobCheckpoints(fakeredis.aioredis.FakeRedis(), prefix="test:verified", ttl_seconds=604800)
    report = await VerifiedReportPipeline(clock=lambda: VERIFIED_AT).build(
        job_id=JOB_ID, request=request, resolved_input=resolved, checkpoints=checkpoints)

    assert report.report_version.report_type == "verified_execution"
    assert report.report_version.report_id == JOB_ID
    assert report.report_version.parent_report_id == PARENT_ID
    assert report.report_version.version_number == resolved.parent_report.report_version.version_number + 1 == 2
    assert report.first_party_performance.gsc.snapshot_id == request.gsc_snapshot_id
    assert report.first_party_performance.ga4.snapshot_id == request.ga4_snapshot_id
    assert all(entry.change_type != "unchanged" for entry in report.version_diff.entries)
    assert report.report_version.copy_model_version == "v22_verified_copy_v1"
    parent_evidence = {item.evidence_id: item for item in resolved.parent_report.evidence_index}
    current_evidence = {item.evidence_id: item for item in report.evidence_index}
    assert all(current_evidence[key] == value for key, value in parent_evidence.items())


@pytest.mark.anyio
async def test_verified_pipeline_has_no_network_collection_or_copy_generation(monkeypatch):
    from verified_pipeline_helpers import resolve_fixture
    from app.competitors_v22.collection_stage import CheckpointedCompetitorCollectionStage
    from app.jobs_v22.copy_provider import DifyControlledCopyProvider
    from app.jobs_v22.customer_public_gbp_stage import SerpApiCustomerPublicGbpProvider
    from app.jobs_v22.site_inventory_stage import CheckpointedSiteInventoryStage

    async def forbidden(*args, **kwargs):
        pytest.fail("Verified pipeline attempted a Prospect collection or copy provider")

    monkeypatch.setattr(CheckpointedSiteInventoryStage, "collect", forbidden)
    monkeypatch.setattr(SerpApiCustomerPublicGbpProvider, "request", forbidden)
    monkeypatch.setattr(CheckpointedCompetitorCollectionStage, "collect", forbidden)
    monkeypatch.setattr(DifyControlledCopyProvider, "generate", forbidden)
    resolved, request = await resolve_fixture()
    report = await VerifiedReportPipeline(clock=lambda: VERIFIED_AT).build(
        job_id=JOB_ID, request=request, resolved_input=resolved,
        checkpoints=JobCheckpoints(fakeredis.aioredis.FakeRedis(),
            prefix="test:verified:no-network", ttl_seconds=604800))
    assert report.report_version.report_type == "verified_execution"


def test_parent_fixture_uses_real_public_findings_and_action_plan():
    payload, _, public_result, public_plan = frozen_public_fixture()
    parent = payload["parent_report"]
    assert parent["findings"] == [item.model_dump(mode="json") for item in public_result.findings]
    assert [item["action_id"] for item in parent["top_actions"]] == [item.action_id for item in public_plan.actions]


@pytest.mark.anyio
async def test_restart_hits_every_stage_checkpoint_and_preserves_canonical_report_bytes():
    from verified_pipeline_helpers import resolve_fixture

    resolved, request = await resolve_fixture()
    checkpoints = JobCheckpoints(fakeredis.aioredis.FakeRedis(), prefix="test:verified", ttl_seconds=604800)
    calls = []
    stage_names = ("public_stage", "first_party_stage", "cross_source_stage",
        "reprioritization_stage", "version_diff_stage", "execution_plan_stage")
    pipeline = VerifiedReportPipeline(clock=lambda: VERIFIED_AT)
    inputs = {}
    for name in stage_names:
        stage = getattr(pipeline, name)
        builder = stage.builder
        def counted(value, *, name=name, builder=builder):
            calls.append(name)
            inputs[name] = value
            return builder(value)
        stage.builder = counted
    first = await pipeline.build(job_id=JOB_ID, request=request, resolved_input=resolved, checkpoints=checkpoints)
    assert calls == list(stage_names)
    assert inputs["public_stage"].evidence_input.context.evaluated_at == resolved.parent_report.report_version.generated_at
    for name in stage_names[1:]:
        assert inputs[name].evaluated_at == VERIFIED_AT
    assert inputs["reprioritization_stage"].public_action_plan.planning_date == resolved.parent_report.report_version.generated_at.date()
    assert inputs["execution_plan_stage"].planning_date == VERIFIED_AT.date()

    def unexpected_builder(value):
        pytest.fail("a completed deterministic stage ran again")
    def unexpected_clock():
        pytest.fail("a resumed job recaptured its evaluation time")
    restarted = VerifiedReportPipeline(clock=unexpected_clock)
    for name in stage_names:
        getattr(restarted, name).builder = unexpected_builder
    second = await restarted.build(job_id=JOB_ID, request=request, resolved_input=resolved, checkpoints=checkpoints)
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    keys = [key.decode() async for key in checkpoints.redis.scan_iter()]
    assert all(str(JOB_ID) in key and str(PARENT_ID) not in key for key in keys)


@pytest.mark.anyio
async def test_reversed_first_party_resolver_order_preserves_all_stage_checkpoint_keys():
    from verified_pipeline_helpers import resolve_fixture

    normal, request = await resolve_fixture()
    reversed_input, reversed_request = await resolve_fixture(reverse_first_party=True)
    normal_redis = fakeredis.aioredis.FakeRedis()
    reversed_redis = fakeredis.aioredis.FakeRedis()
    normal_checkpoints = JobCheckpoints(normal_redis, prefix="test:semantic-order", ttl_seconds=604800)
    reversed_checkpoints = JobCheckpoints(reversed_redis, prefix="test:semantic-order", ttl_seconds=604800)

    first = await VerifiedReportPipeline(clock=lambda: VERIFIED_AT).build(
        job_id=JOB_ID, request=request, resolved_input=normal, checkpoints=normal_checkpoints)
    second = await VerifiedReportPipeline(clock=lambda: VERIFIED_AT).build(
        job_id=JOB_ID, request=reversed_request, resolved_input=reversed_input,
        checkpoints=reversed_checkpoints)

    normal_keys = {key.decode() async for key in normal_redis.scan_iter()}
    reversed_keys = {key.decode() async for key in reversed_redis.scan_iter()}
    assert normal_keys == reversed_keys
    # Six stage results plus the separately checkpointed evaluation timestamp.
    assert len(normal_keys) == 7
    assert canonical_json_bytes(first) == canonical_json_bytes(second)


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["parent", "public_row", "public_reference", "resigned_public",
    "source", "snapshot", "checksum", "copy"])
async def test_tampering_fails_before_any_stage_checkpoint_or_report(change):
    from verified_pipeline_helpers import resolve_fixture

    resolved, request = await resolve_fixture()
    if change == "parent":
        resolved.parent_report.findings[0].statement = "Altered parent Finding"
    elif change == "public_row":
        resolved.public_gbp_snapshot.normalized_payload.health_status = "unavailable"
    elif change == "public_reference":
        resolved.public_gbp_snapshot.reference.entity_keys[0].value = "tampered"
    elif change == "resigned_public":
        resolved.public_gbp_snapshot.normalized_payload.record.fields.business_name.value = "Different Business"
        resolved.public_gbp_snapshot.payload_checksum = request_digest(
            resolved.public_gbp_snapshot.normalized_payload)
    elif change == "source":
        resolved.site_snapshot.source_type = "serp"
    elif change == "snapshot":
        resolved.first_party_snapshots[0].normalized_payload["resource_id"] = "sc-domain:wrong.test"
    elif change == "checksum":
        request.input_checksum = "sha256:" + "0" * 64
    else:
        resolved = resolved.payload.model_copy(deep=True)
    checkpoints = JobCheckpoints(fakeredis.aioredis.FakeRedis(), prefix="test:verified", ttl_seconds=604800)
    with pytest.raises(DeterministicJobError):
        await VerifiedReportPipeline(clock=lambda: VERIFIED_AT).build(
            job_id=JOB_ID, request=request, resolved_input=resolved, checkpoints=checkpoints)
    assert await checkpoints.redis.dbsize() == 0


@pytest.mark.anyio
async def test_pipeline_capability_seal_rejects_resigned_nested_gsc_mutation():
    from verified_pipeline_helpers import resolve_fixture

    resolved, request = await resolve_fixture()
    gsc = next(item for item in resolved.first_party_snapshots if item.source_type == "gsc")
    gsc.normalized_payload["current"]["totals"]["clicks"] = 999
    gsc.payload_checksum = request_digest(gsc.normalized_payload)
    checkpoints = JobCheckpoints(fakeredis.aioredis.FakeRedis(),
        prefix="test:verified:sealed", ttl_seconds=604800)
    with pytest.raises(DeterministicJobError):
        await VerifiedReportPipeline(clock=lambda: VERIFIED_AT).build(
            job_id=JOB_ID, request=request, resolved_input=resolved, checkpoints=checkpoints)
    assert await checkpoints.redis.dbsize() == 0
