"""NUL normalization belongs to new collection, never to digest/replay."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import logging
from unittest.mock import AsyncMock
from uuid import UUID

import fakeredis.aioredis
import pytest

from app.jobs_v22.collection_json import normalize_collection_json
from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError

JOB = UUID('11111111-2222-4333-8444-555566667777')
KINDS = ['site', 'serp', 'competitor', 'public_gbp', 'rendered']

@pytest.fixture
def anyio_backend():
    return 'asyncio'

@pytest.mark.parametrize('source', KINDS)
def test_recursive_keys_values_counts_privacy_and_no_mutation(source, caplog):
    raw = {'private\0key': [{'nested': 'secret\0value\0', 'literal': r'\u0000'}], 'bool': True, 'null': None, 'number': 3.5}
    before = deepcopy(raw)
    with caplog.at_level(logging.INFO, logger='app.jobs_v22.collection_json'):
        clean = normalize_collection_json(raw, source=source, job_id=JOB)
    assert clean['privatekey'][0] == {'nested': 'secretvalue', 'literal': r'\u0000'}
    assert raw == before
    assert '\\u0000' not in canonical_json_bytes(clean).decode().replace('\\\\u0000', '')
    assert [r.getMessage() for r in caplog.records] == [
        f'collection JSON normalized source={source} removed_characters=3 job_id_suffix=66667777']
    assert all(secret not in caplog.text for secret in ['private', 'secret', str(JOB)])

@pytest.mark.parametrize('source', KINDS)
def test_nul_free_canonical_and_both_digests_are_byte_identical(source):
    raw = {'z': [None, True, 1.25, -0.0, 'é 🦉', r'\u0000'], 'a': {'key': 'value'}}
    old = (canonical_json_bytes(raw), verified_canonical_json_bytes(raw), request_digest(raw), verified_request_digest(raw))
    clean = normalize_collection_json(raw, source=source, job_id=JOB)
    assert clean is raw
    assert (canonical_json_bytes(clean), verified_canonical_json_bytes(clean), request_digest(clean), verified_request_digest(clean)) == old

@pytest.mark.parametrize('keys', [('key', 'ke\0y'), ('ke\0y', 'key')])
def test_key_collision_never_silently_overwrites(keys):
    raw = {keys[0]: 'first', keys[1]: 'second'}
    with pytest.raises(DeterministicJobError) as caught:
        normalize_collection_json(raw, source='site', job_id=JOB)
    assert caught.value.error_code == 'COLLECTION_JSON_KEY_COLLISION'
    assert len(raw) == 2

@pytest.mark.anyio
@pytest.mark.parametrize('source', ['site', 'competitor'])
async def test_static_site_collection_cleans_before_snapshot_checkpoint(source, monkeypatch, caplog):
    from test_v22_site_inventory_stage import stage, RecordingFetcher, prospect_request
    from app.jobs_v22.checkpoints import JobCheckpoints
    from app.jobs_v22.site_inventory_stage import SiteInventoryCollector
    from site_business_helpers import request as site_request
    snapshot = site_request('<h1>private\0body</h1>').source.payload
    monkeypatch.setattr(SiteInventoryCollector, 'collect', AsyncMock(return_value=snapshot))
    redis = fakeredis.aioredis.FakeRedis()
    cp = JobCheckpoints(redis, prefix='nul', ttl_seconds=600)
    collector = stage(RecordingFetcher(), None)
    try:
        with caplog.at_level(logging.INFO, logger='app.jobs_v22.collection_json'):
            if source == 'site':
                result = await collector.collect(job_id=JOB, request=prospect_request(), checkpoints=cp)
            else:
                result = await collector.collect_inventory_for_site(job_id=JOB, site_url='https://example.test/', primary_service='Plumbing',
                    target_market='Austin', discovery_limit=2, deep_analysis_limit=2, gsc_priorities=[], checkpoints=cp, checkpoint_namespace='competitor:1')
        assert '\\u0000' not in canonical_json_bytes(result).decode()
        assert '\0' in snapshot.selected_pages[0].deep_snapshot.html
        saved = [await redis.get(key) async for key in redis.scan_iter()]
        assert saved and all(b'\\u0000' not in value for value in saved)
        assert f'source={source}' in caplog.text
    finally:
        await redis.aclose()

@pytest.mark.anyio
async def test_serp_cleans_provider_keys_before_normalization_and_response_checksum():
    from app.collectors.serp_market import SerpProviderResponse
    from app.jobs_v22.serp_market_stage import CheckpointedSerpMarketStage
    from test_v22_serp_market_stage import checkpoints, payload_for, NOW, prospect_request, UnusedLocationProvider
    class Provider:
        async def search(self, call, *, before_attempt, after_attempt=None):
            await before_attempt(1, 'a'*12)
            if after_attempt:
                await after_attempt(1, 'a'*12, None)
            payload = payload_for(call)
            if 'organic_results' in payload:
                payload['org\0anic_results'] = payload.pop('organic_results')
                payload['org\0anic_results'][0]['title'] = 'private\0result'
            return SerpProviderResponse(payload, started_at=NOW, completed_at=NOW)
    cp = checkpoints()
    try:
        result = await CheckpointedSerpMarketStage(location_provider=UnusedLocationProvider(), search_provider=Provider(), clock=lambda: NOW).collect(
            job_id=JOB, request=prospect_request(), checkpoints=cp)
        assert '\\u0000' not in canonical_json_bytes(result).decode()
        assert any(record.display_name == 'privateresult' for run in result.query_runs for record in run.results)
        for key in [key async for key in cp.redis.scan_iter()]:
            raw = await cp.redis.get(key)
            assert b'\\u0000' not in raw
            if b'"payload_checksum"' in raw:
                import json
                checkpoint = json.loads(raw)
                assert checkpoint['payload_checksum'] == request_digest(checkpoint['payload'])
    finally:
        await cp.redis.aclose()

@pytest.mark.anyio
async def test_competitor_reviews_clean_keys_and_values_before_checkpoint():
    from app.competitors_v22.public_profile_stage import CheckpointedPublicProfileStage, SharedProviderAttemptBudget
    from app.jobs_v22.checkpoints import JobCheckpoints
    from test_v22_competitor_public_profile_stage import RecordingProvider, competitor, complete_market, NOW
    class Provider(RecordingProvider):
        async def request(self, *args, **kwargs):
            result = await super().request(*args, **kwargs)
            result['re\0views'] = result.pop('reviews')
            result['re\0views'][0]['snippet'] = 'private\0review'
            return result
    redis = fakeredis.aioredis.FakeRedis()
    cp = JobCheckpoints(redis, prefix='nul', ttl_seconds=600)
    try:
        result = await CheckpointedPublicProfileStage(Provider(), clock=lambda: NOW).collect_one(job_id=JOB, competitor=competitor(),
            market_snapshot=complete_market(), language='en', checkpoints=cp, budget=SharedProviderAttemptBudget(cp, JOB))
        assert result.reviews and all('\0' not in review.text for review in result.reviews)
        assert result.reviews[0].text == 'privatereview'
        for key in [key async for key in redis.scan_iter()]:
            assert b'\\u0000' not in await redis.get(key)
    finally:
        await redis.aclose()

@pytest.mark.anyio
async def test_customer_gbp_clean_before_snapshot_checksum_preserves_raw_response_provenance():
    from app.jobs_v22.customer_public_gbp_stage import CheckpointedCustomerPublicGbpStage
    from app.jobs_v22.checkpoints import JobCheckpoints
    from test_v22_customer_public_gbp_stage import payload, provider_response, request, NOW
    raw = payload()
    raw['pla\0ce_results'] = raw.pop('place_results')
    raw['pla\0ce_results']['title'] = 'Example\0 Plumbing'
    response = provider_response(raw)
    provider = type('Provider', (), {'request': AsyncMock(return_value=response)})()
    redis = fakeredis.aioredis.FakeRedis()
    cp = JobCheckpoints(redis, prefix='nul', ttl_seconds=600)
    try:
        result = await CheckpointedCustomerPublicGbpStage(provider, clock=lambda: NOW).collect(job_id=JOB, request=request(),
            submitted_at=NOW-timedelta(minutes=1), checkpoints=cp)
        assert result.snapshot.record.fields.business_name.value == 'Example Plumbing'
        assert result.snapshot.response_checksum == response.response_checksum
        assert '\\u0000' not in canonical_json_bytes(result).decode()
        assert '\0' in raw['pla\0ce_results']['title']
    finally:
        await redis.aclose()

@pytest.mark.anyio
async def test_rendered_html_clean_before_content_checksum_and_checkpoint():
    from test_p0_04_render_fallback import setup
    from app.collectors.site_render_firecrawl import FirecrawlRenderResult
    redis, renderer, stage, args = await setup()
    renderer.scrape = AsyncMock(return_value=FirecrawlRenderResult(html='<p>private\0render</p>', final_url=args['business'].site_url, status_code=200))
    try:
        result = await stage.collect(**args)
        page = result.pages[0].deep_snapshot
        assert page.html == '<p>privaterender</p>'
        assert page.content_checksum == 'sha256:' + hashlib.sha256(page.html.encode()).hexdigest()
        assert page.response_bytes == len(page.html.encode())
        assert '\\u0000' not in canonical_json_bytes(result).decode()
        assert await stage.collect(**args) == result
        assert renderer.scrape.await_count == 1
    finally:
        await redis.aclose()

def test_real_five_source_graph_without_nuls_keeps_all_original_bytes_and_digests():
    from test_v22_capability_acquisition_preparation import inputs
    from app.jobs_v22.capability_acquisition_input import build_acquisition_input
    from test_p0_04_render_fallback import rendered_source
    args, _, _ = inputs()
    manifest, value = build_acquisition_input(**args)
    static = next(s for s in value.evidence_input.sources if s.kind == 'site')
    sources = [*value.evidence_input.sources, rendered_source(static, failed=True)]
    assert {s.kind for s in sources} == set(KINDS)
    for source in sources:
        raw = source.payload.model_dump(mode='json')
        before = (canonical_json_bytes(raw), verified_canonical_json_bytes(raw), request_digest(raw), verified_request_digest(raw))
        clean = normalize_collection_json(raw, source=source.kind, job_id=args['job_id'])
        assert clean is raw
        assert (canonical_json_bytes(clean), verified_canonical_json_bytes(clean), request_digest(clean), verified_request_digest(clean)) == before
        assert request_digest(clean) == source.binding.payload_checksum

@pytest.mark.anyio
async def test_competitor_website_does_not_downgrade_key_collision_to_unavailable():
    from app.competitors_v22.site_stage import CheckpointedCompetitorSiteStage
    from app.jobs_v22.collection_json import CollectionJsonKeyCollision
    from test_v22_competitor_public_profile_stage import competitor
    from test_v22_competitor_collection_stage import request
    collector = type('Collector', (), {'collect_inventory_for_site': AsyncMock(side_effect=CollectionJsonKeyCollision())})()
    with pytest.raises(CollectionJsonKeyCollision):
        await CheckpointedCompetitorSiteStage(collector).collect(job_id=JOB, competitors=[competitor()], primary_service='Plumbing',
            target_market=request().target_market, max_pages_each=1, checkpoints=None)

@pytest.mark.anyio
async def test_competitor_profile_does_not_downgrade_key_collision_to_partial():
    from app.competitors_v22.public_profile_stage import CheckpointedPublicProfileStage, SharedProviderAttemptBudget
    from app.jobs_v22.collection_json import CollectionJsonKeyCollision
    from app.jobs_v22.checkpoints import JobCheckpoints
    from test_v22_competitor_public_profile_stage import RecordingProvider, competitor, complete_market, NOW
    class Provider(RecordingProvider):
        async def request(self, *args, **kwargs):
            result = await super().request(*args, **kwargs)
            return {**result, 're\0views': []}
    redis = fakeredis.aioredis.FakeRedis()
    cp = JobCheckpoints(redis, prefix='nul', ttl_seconds=600)
    try:
        with pytest.raises(CollectionJsonKeyCollision):
            await CheckpointedPublicProfileStage(Provider(), clock=lambda: NOW).collect_one(job_id=JOB, competitor=competitor(),
                market_snapshot=complete_market(), language='en', checkpoints=cp, budget=SharedProviderAttemptBudget(cp, JOB))
    finally:
        await redis.aclose()
