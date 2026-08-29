from __future__ import annotations

from datetime import timedelta

import fakeredis.aioredis
import pytest

from app.competitors_v22.market_store import (
    CompetitorMarketSnapshotError,
    SharedMarketSnapshotStore,
)
from test_v22_competitor_candidates import business_records, snapshot
from test_v22_competitor_models import DIGEST, JOB_ID, NOW


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_shared_market_snapshot_round_trip_and_ttl() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    store = SharedMarketSnapshotStore(redis, prefix="test:v22", ttl_seconds=86_400)
    market = snapshot(business_records("Rival Plumbing", "rival.example", 700))

    saved = await store.save(input_digest=DIGEST, snapshot=market, now=NOW)
    loaded = await store.get(input_digest=DIGEST, now=NOW + timedelta(hours=23))

    assert loaded == saved
    assert loaded.snapshot == market
    assert loaded.source_job_id == market.job_id
    assert loaded.expires_at == NOW + timedelta(hours=24)
    assert await store.get(input_digest=DIGEST, now=NOW + timedelta(hours=25)) is None
    assert "emergency plumber" not in store.key(DIGEST)


@pytest.mark.anyio
async def test_shared_market_snapshot_rejects_corruption() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    store = SharedMarketSnapshotStore(redis, prefix="test:v22", ttl_seconds=86_400)
    await redis.set(store.key(DIGEST), b'{"bad":"payload"}', ex=86_400)

    with pytest.raises(CompetitorMarketSnapshotError):
        await store.get(input_digest=DIGEST, now=NOW)


@pytest.mark.anyio
async def test_first_valid_shared_snapshot_wins() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    store = SharedMarketSnapshotStore(redis, prefix="test:v22", ttl_seconds=86_400)
    market = snapshot(business_records("Rival Plumbing", "rival.example", 710))

    first = await store.save(input_digest=DIGEST, snapshot=market, now=NOW)
    replayed = await store.save(
        input_digest=DIGEST,
        snapshot=market,
        now=NOW + timedelta(minutes=1),
    )

    assert replayed == first
    assert replayed.created_at == NOW
