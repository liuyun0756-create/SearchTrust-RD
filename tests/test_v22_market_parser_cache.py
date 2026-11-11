"""Parser upgrades replace reusable cache slots, never confirmed observations."""
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5, uuid4

import fakeredis.aioredis
import pytest

from app.collectors.serp_market import SERP_MARKET_PARSER_VERSION
from app.competitors_v22 import market_store as store_module
from app.competitors_v22.market_store import SharedMarketSnapshotStore, CompetitorMarketSnapshotError
from app.competitors_v22.models import SharedMarketSnapshot
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from test_v22_competitor_candidates import business_records, snapshot
from test_v22_competitor_models import DIGEST, NOW, discovery_request
from test_v22_competitor_discovery_service import RecordingMarketStage, market_with_three_competitors, FIRST_JOB, SECOND_JOB
from app.competitors_v22.discovery_service import CompetitorDiscoveryService, competitor_discovery_input_digest

@pytest.fixture
def anyio_backend():
    return 'asyncio'


def legacy(payload, digest=DIGEST):
    checksum = request_digest(payload)
    return SharedMarketSnapshot(schema_version='competitor_shared_market_v1',
        snapshot_id=uuid5(NAMESPACE_URL, f'searchtrust:v22:market:{digest}:{checksum}'),
        source_job_id=payload.job_id, input_digest=digest, snapshot_checksum=checksum,
        created_at=NOW, expires_at=NOW+timedelta(hours=24), snapshot=payload)


@pytest.mark.anyio
async def test_new_save_uses_version_key_and_keeps_legacy_bytes_checksum_expiry_and_ttl():
    redis = fakeredis.aioredis.FakeRedis()
    store = SharedMarketSnapshotStore(redis,prefix='parser-test',ttl_seconds=86400)
    old = legacy(snapshot(business_records('Old', 'old.example', 700)))
    raw = canonical_json_bytes(old)
    try:
        await redis.set(store.legacy_key(DIGEST),raw,ex=86400)
        ttl = await redis.pttl(store.legacy_key(DIGEST))
        assert await store.get_current(input_digest=DIGEST,now=NOW) is None
        assert await store.get(input_digest=DIGEST,now=NOW) == old
        fresh = snapshot(business_records('New', 'new.example', 710)).model_copy(update={'job_id':uuid4()})
        current = await store.save(input_digest=DIGEST,snapshot=fresh,now=NOW)
        assert SERP_MARKET_PARSER_VERSION == 'serp_market_v3' and SERP_MARKET_PARSER_VERSION in store.key(DIGEST)
        assert current.snapshot_id == uuid5(NAMESPACE_URL, f'searchtrust:v22:market:{DIGEST}:{request_digest(fresh)}')
        assert current.snapshot_id != old.snapshot_id
        assert await store.get(input_digest=DIGEST,now=NOW) == current
        pinned = await store.get(input_digest=DIGEST,now=NOW,snapshot_id=old.snapshot_id)
        assert canonical_json_bytes(pinned) == raw
        assert pinned.snapshot_checksum == old.snapshot_checksum and pinned.expires_at == old.expires_at
        assert await redis.get(store.legacy_key(DIGEST)) == raw
        assert 0 < await redis.pttl(store.legacy_key(DIGEST)) <= ttl
    finally:
        await redis.aclose()


@pytest.mark.anyio
async def test_current_cache_wins_and_repeated_save_returns_original_capture():
    redis = fakeredis.aioredis.FakeRedis()
    store = SharedMarketSnapshotStore(redis,prefix='parser-test',ttl_seconds=86400)
    try:
        old = legacy(snapshot(business_records('Old', 'old.example', 700)))
        await redis.set(store.legacy_key(DIGEST),canonical_json_bytes(old),ex=86400)
        first = await store.save(input_digest=DIGEST,snapshot=snapshot(business_records('New', 'new.example', 710)),now=NOW)
        again = await store.save(input_digest=DIGEST,snapshot=snapshot(business_records('Ignored', 'ignored.example', 720)),now=NOW+timedelta(hours=1))
        assert again == first and again.expires_at == first.expires_at
        assert await store.get(input_digest=DIGEST,now=NOW) == first
        await redis.set(store.key(DIGEST),b'{"invalid":true}',ex=86400)
        with pytest.raises(CompetitorMarketSnapshotError):
            await store.get(input_digest=DIGEST,now=NOW)
    finally:
        await redis.aclose()


@pytest.mark.anyio
async def test_old_expired_key_is_never_deleted_or_extended():
    redis = fakeredis.aioredis.FakeRedis()
    store = SharedMarketSnapshotStore(redis,prefix='parser-test',ttl_seconds=86400)
    old = legacy(snapshot(business_records('Old', 'old.example', 700)))
    raw = canonical_json_bytes(old)
    try:
        await redis.set(store.legacy_key(DIGEST),raw,ex=86400)
        assert await store.get(input_digest=DIGEST,now=NOW+timedelta(days=2)) is None
        assert await redis.get(store.legacy_key(DIGEST)) == raw
    finally:
        await redis.aclose()


@pytest.mark.anyio
async def test_pinned_new_snapshot_remains_identical_after_a_later_parser_namespace(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis()
    store = SharedMarketSnapshotStore(redis,prefix='parser-test',ttl_seconds=86400)
    try:
        first = await store.save(input_digest=DIGEST,snapshot=snapshot(business_records('New', 'new.example', 710)),now=NOW)
        raw = canonical_json_bytes(first)
        monkeypatch.setattr(store_module,'SERP_MARKET_PARSER_VERSION','hypothetical_next_parser')
        assert await store.get_current(input_digest=DIGEST,now=NOW) is None
        pinned = await store.get(input_digest=DIGEST,now=NOW,snapshot_id=first.snapshot_id)
        assert canonical_json_bytes(pinned) == raw
        assert await redis.get(store.snapshot_key(first.snapshot_id)) == raw
    finally:
        await redis.aclose()


@pytest.mark.anyio
async def test_first_discovery_collects_despite_legacy_cache_then_reuses_current_without_second_collection():
    redis = fakeredis.aioredis.FakeRedis()
    store = SharedMarketSnapshotStore(redis,prefix='parser-test',ttl_seconds=86400)
    req = discovery_request()
    digest = competitor_discovery_input_digest(req)
    old = legacy(market_with_three_competitors(),digest)
    raw = canonical_json_bytes(old)
    stage = RecordingMarketStage(market_with_three_competitors())
    service = CompetitorDiscoveryService(market_stage=stage,market_store=store,clock=lambda:NOW)
    cp = JobCheckpoints(redis,prefix='parser-test',ttl_seconds=86400)
    try:
        await redis.set(store.legacy_key(digest),raw,ex=86400)
        first = await service.discover(discovery_job_id=FIRST_JOB,request=req,checkpoints=cp)
        second = await service.discover(discovery_job_id=SECOND_JOB,request=req,checkpoints=cp)
        assert len(stage.calls) == 1
        assert first.market_snapshot_id != old.snapshot_id
        assert second.market_snapshot_id == first.market_snapshot_id
        assert second.market_snapshot_checksum == first.market_snapshot_checksum
        assert await redis.get(store.legacy_key(digest)) == raw
        assert canonical_json_bytes(await store.get(input_digest=digest,now=NOW,snapshot_id=old.snapshot_id)) == raw
    finally:
        await redis.aclose()


@pytest.mark.anyio
async def test_v3_discovery_bypasses_v2_cache_and_preserves_pinned_v2_snapshot(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis()
    store = SharedMarketSnapshotStore(redis, prefix='parser-test', ttl_seconds=86400)
    req = discovery_request()
    digest = competitor_discovery_input_digest(req)
    stage = RecordingMarketStage(market_with_three_competitors())
    service = CompetitorDiscoveryService(market_stage=stage, market_store=store, clock=lambda: NOW)
    cp = JobCheckpoints(redis, prefix='parser-test', ttl_seconds=86400)
    try:
        monkeypatch.setattr(store_module, 'SERP_MARKET_PARSER_VERSION', 'serp_market_v2')
        old = await store.save(input_digest=digest, snapshot=market_with_three_competitors().model_copy(
            update={'job_id': SECOND_JOB}), now=NOW)
        old_key = store.key(digest)
        old_bytes = await redis.get(old_key)
        old_ttl = await redis.pttl(old_key)
        monkeypatch.setattr(store_module, 'SERP_MARKET_PARSER_VERSION', SERP_MARKET_PARSER_VERSION)
        assert SERP_MARKET_PARSER_VERSION == 'serp_market_v3'
        assert await store.get_current(input_digest=digest, now=NOW) is None
        first = await service.discover(discovery_job_id=FIRST_JOB, request=req, checkpoints=cp)
        second = await service.discover(discovery_job_id=SECOND_JOB, request=req, checkpoints=cp)
        assert len(stage.calls) == 1
        assert first.market_snapshot_id != old.snapshot_id
        assert first.market_snapshot_id == second.market_snapshot_id
        assert first.market_snapshot_checksum == second.market_snapshot_checksum
        assert await redis.get(store.key(digest)) is not None
        assert await redis.get(old_key) == old_bytes
        assert 0 < await redis.pttl(old_key) <= old_ttl
        pinned = await store.get(input_digest=digest, now=NOW, snapshot_id=old.snapshot_id)
        assert canonical_json_bytes(pinned) == old_bytes
        assert pinned.snapshot_checksum == old.snapshot_checksum
        assert pinned.expires_at == old.expires_at
        assert await redis.get(store.snapshot_key(old.snapshot_id)) == old_bytes
    finally:
        await redis.aclose()
