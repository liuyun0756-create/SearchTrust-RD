from copy import deepcopy
from datetime import timedelta
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

import fakeredis.aioredis
import pytest

from app.competitors_v22.market_store import SharedMarketSnapshotStore
from app.jobs_v22.capability_acquisition_input import build_acquisition_input
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.evidence_bindings import validate_sources
from app.report_v22.evidence_errors import EvidenceError
from app.report_v22.shared_market_binding import case_market_binding_id
from test_v22_capability_acquisition_preparation import inputs
from test_v22_competitor_candidates import business_records, snapshot
from test_v22_competitor_models import DIGEST, NOW


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_binding_identity_vector():
    assert str(case_market_binding_id(UUID(int=1), UUID(int=2))) == "65d722a8-874d-3176-9ec7-1e9269194fe5"


def test_two_cases_keep_same_original_market_but_distinct_storage_and_evidence_references():
    first, _, _ = inputs()
    second = deepcopy(first)
    second["request"].case_id = uuid4()
    second["business"].case_id = second["request"].case_id
    second["business"].id = uuid4()
    second["business"].user_id = second["user_id"] = uuid4()
    second["job_id"] = uuid4()
    second["collected"].competitor_collection.job_id = second["job_id"]
    second["collected"].discovery.case_id = second["request"].case_id
    reference = second["collected"].customer_public_gbp.reference
    reference.case_id = second["request"].case_id
    second["collected"].customer_public_gbp.snapshot.subject_reference_checksum = request_digest(reference)
    original = canonical_json_bytes(first["collected"].shared_market)
    results = []
    for args in [first, second]:
        manifest, value = build_acquisition_input(**args)
        market = next(s for s in value.evidence_input.sources if s.kind == "serp")
        competitor = next(s for s in value.evidence_input.sources if s.kind == "competitor")
        assert canonical_json_bytes(market.shared_snapshot) == original
        assert competitor.payload.market_snapshot_id == market.shared_snapshot.snapshot_id
        assert market.binding.snapshot_id != market.shared_snapshot.snapshot_id
        assert market.binding.expires_at == args["collected"].shared_market.expires_at
        assert market.binding.fetched_at == args["collected"].shared_market.snapshot.completed_at
        report = build_public_capability_report(manifest, value)
        assert all(e.snapshot_id in {s.snapshot_id for s in manifest.snapshots} for e in report.evidence)
        results.append(market.binding.snapshot_id)
    assert results[0] != results[1]
    assert canonical_json_bytes(first["collected"].shared_market) == original


@pytest.mark.parametrize("mutation", ["random-binding", "other-case", "origin", "competitor-origin", "freshened-expiry"])
def test_local_binding_cannot_break_source_lineage(mutation):
    args, _, _ = inputs(); _, value = build_acquisition_input(**args)
    market = next(s for s in value.evidence_input.sources if s.kind == "serp")
    if mutation == "random-binding":market.binding.snapshot_id = uuid4()
    if mutation == "other-case":market.binding.snapshot_id = case_market_binding_id(uuid4(), market.shared_snapshot.snapshot_id)
    if mutation == "origin":market.shared_snapshot.snapshot_id = uuid4()
    if mutation == "competitor-origin":
        rival = next(s for s in value.evidence_input.sources if s.kind == "competitor")
        rival.payload.market_snapshot_id = uuid4()
        rival.binding.payload_checksum = request_digest(rival.payload)
    if mutation == "freshened-expiry":market.binding.expires_at += timedelta(hours=1)
    with pytest.raises(EvidenceError):validate_sources(value.evidence_input)


@pytest.mark.anyio
async def test_cache_hit_preserves_legacy_source_and_new_capture_gets_new_identity_after_expiry():
    redis = fakeredis.aioredis.FakeRedis()
    store = SharedMarketSnapshotStore(redis, prefix="test:v22", ttl_seconds=60)
    market = snapshot(business_records("Rival Plumbing", "rival.example", 700))
    try:
        first = await store.save(input_digest=DIGEST, snapshot=market, now=NOW)
        legacy = first.model_copy(update={"snapshot_id":uuid5(NAMESPACE_URL, f"searchtrust:v22:{DIGEST}")})
        await redis.set(store.key(DIGEST), canonical_json_bytes(legacy), ex=60)
        next_capture = market.model_copy(update={"job_id":uuid4()})
        assert await store.save(input_digest=DIGEST, snapshot=next_capture, now=NOW+timedelta(seconds=1)) == legacy
        later = await store.save(input_digest=DIGEST, snapshot=next_capture, now=NOW+timedelta(seconds=61))
        assert later.snapshot_id not in {first.snapshot_id, legacy.snapshot_id}
        assert later.snapshot == next_capture
        assert later.source_job_id == next_capture.job_id
        assert later.snapshot.completed_at == next_capture.completed_at
        assert legacy.snapshot == market
    finally:
        await redis.aclose()
