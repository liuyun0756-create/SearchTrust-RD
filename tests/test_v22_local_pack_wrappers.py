"""Google local-pack wrappers are distinct from absent or malformed sections."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.collectors.serp_market import SerpMarketCollector, SerpProviderResponse, sanitize_serp_payload
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from test_v22_serp_market_collector import FixtureProvider, load_fixture, plan, JOB_ID, NOW


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
@pytest.mark.parametrize('shape,expected_pack', [('list', 2), ('dict', 2), ('null', 0)])
async def test_three_queries_keep_list_wrapped_and_null_sections_distinct(shape, expected_pack):
    class Provider(FixtureProvider):
        async def search(self, call):
            response = await super().search(call)
            if call.engine == 'google':
                raw = response.payload
                raw['local_results'] = {'places': raw['local_results']} if shape == 'dict' else None if shape == 'null' else raw['local_results']
            return response
    search_plan = plan()
    result = await SerpMarketCollector(provider=Provider(), clock=lambda: NOW).collect(job_id=JOB_ID, plan=search_plan, location_resolution_calls=0)
    for run in result.query_runs:
        counts = {kind: sum(r.result_type == kind for r in run.results) for kind in ('maps', 'local_pack', 'organic')}
        assert counts == {'maps': 2, 'local_pack': expected_pack, 'organic': 2}
        assert 'SERP_LOCAL_PACK_SECTION_INVALID' not in run.limitations
        assert run.google_call.status == 'succeeded'
        local = [r for r in run.results if r.result_type == 'local_pack']
        assert [r.position for r in local] == list(range(1, expected_pack+1))
        assert all(r.rank_source == 'provider_position' and r.response_checksum == run.google_call.response_checksum for r in local)
    assert all('local_pack' in call.result_types for call in search_plan.calls if call.engine == 'google')


def test_sanitized_list_bytes_unchanged_and_wrapped_payload_is_idempotent():
    raw = load_fixture('google-search.json')
    expected = sanitize_serp_payload(raw, 'google')
    wrapped = deepcopy(raw)
    wrapped['local_results'] = {'places': wrapped['local_results'], 'more_locations_link': 'https://www.google.com/search?tbm=lcl'}
    clean = sanitize_serp_payload(wrapped, 'google')
    assert clean == expected
    assert canonical_json_bytes(clean) == canonical_json_bytes(expected)
    assert request_digest(clean) == request_digest(expected)
    assert sanitize_serp_payload(clean, 'google') == clean
    assert isinstance(wrapped['local_results'], dict)


@pytest.mark.anyio
async def test_real_frozen_partial_wrapper_is_not_invalid_and_does_not_invent_names():
    raw = json.loads((Path(__file__).parent/'fixtures/v22_serp_market/local-pack-partial-redacted.json').read_text())
    class Provider(FixtureProvider):
        async def search(self, call):
            response = await super().search(call)
            if call.engine == 'google':
                response.payload['local_results'] = deepcopy(raw['local_results'])
            return response
    result = await SerpMarketCollector(provider=Provider(), clock=lambda: NOW).collect(job_id=JOB_ID, plan=plan(), location_resolution_calls=0)
    for run in result.query_runs:
        assert not [r for r in run.results if r.result_type == 'local_pack']
        assert 'SERP_LOCAL_PACK_SECTION_INVALID' not in run.limitations
        assert 'SERP_LOCAL_PACK_RESULT_SKIPPED' in run.limitations


@pytest.mark.anyio
@pytest.mark.parametrize('wrapper', [{}, {'places': None}, {'places': 'bad'}])
async def test_malformed_wrappers_remain_explicitly_invalid(wrapper):
    class Provider(FixtureProvider):
        async def search(self, call):
            response = await super().search(call)
            if call.engine == 'google':
                response.payload['local_results'] = wrapper
            return response
    result = await SerpMarketCollector(provider=Provider(), clock=lambda: NOW).collect(job_id=JOB_ID, plan=plan(), location_resolution_calls=0)
    assert all('SERP_LOCAL_PACK_SECTION_INVALID' in run.limitations for run in result.query_runs)


def test_maps_does_not_reinterpret_google_search_wrappers():
    raw = load_fixture('google-maps.json')
    raw['local_results'] = {'places': raw['local_results']}
    assert sanitize_serp_payload(raw, 'google_maps')['local_results'] == 'invalid'


@pytest.mark.anyio
async def test_checkpoint_stage_binds_wrapped_results_and_reuses_exact_saved_checksums():
    from app.jobs_v22.serp_market_stage import CheckpointedSerpMarketStage
    from test_v22_serp_market_stage import SuccessfulSearchProvider, UnusedLocationProvider, prospect_request, checkpoints
    class Provider(SuccessfulSearchProvider):
        async def search(self, call, **kwargs):
            response = await super().search(call, **kwargs)
            if call.engine == 'google':
                response.payload['local_results'] = {'places': response.payload['local_results']}
            return response
    provider = Provider()
    saved = checkpoints()
    stage = CheckpointedSerpMarketStage(search_provider=provider, location_provider=UnusedLocationProvider(), clock=lambda: NOW)
    try:
        result = await stage.collect(job_id=JOB_ID, request=prospect_request(), checkpoints=saved)
        before = canonical_json_bytes(result)
        repeated = await stage.collect(job_id=JOB_ID, request=prospect_request(), checkpoints=saved)
        assert canonical_json_bytes(repeated) == before and provider.network_calls == 6
        assert all(sum(r.result_type == 'local_pack' for r in run.results) == 2 for run in result.query_runs)
        for key in [key async for key in saved.redis.scan_iter()]:
            raw = json.loads(await saved.redis.get(key))
            if raw.get('schema_version') == 'serp_provider_response_v1':
                assert raw['payload_checksum'] == request_digest(raw['payload'])
                assert sanitize_serp_payload(raw['payload'], raw['engine']) == raw['payload']
    finally:
        await saved.redis.aclose()
