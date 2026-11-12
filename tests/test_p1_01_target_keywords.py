"""Phase 2 confirmed queries: no provider, no new rules or frozen mutations."""
from copy import deepcopy
from unittest.mock import AsyncMock, patch
import json
import httpx
import pytest
from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest, request_digest
from app.jobs_v22.target_keywords import TargetKeywordBinding
from app.competitors_v22.selection import AnalysisRequestEnvelope
from test_p1_01_keyword_visibility import keyword_input, reseal
from findings_helpers import seal

CONFIRMED_QUERIES=['Custom Plumbing Austin','My Exact near me','Repair & Install Austin']

def confirmed_input(queries=CONFIRMED_QUERIES):
    m,q=keyword_input((True,False,None))
    q.evidence_input.context.queries=list(queries)
    serp=next(s for s in q.evidence_input.sources if s.kind=='serp')
    serp.payload.queries=list(queries)
    for run,query in zip(serp.payload.query_runs,queries):
        run.query=query;run.maps_call.query=query;run.google_call.query=query
        for record in run.results:
            record.query=query;record.display_name=query+' Austin';record.snippet=query+' Austin'
    serp=seal(serp)
    q.evidence_input.sources=[serp if s.kind=='serp' else s for s in q.evidence_input.sources]
    for source in q.evidence_input.sources:
        if source.kind=='competitor':
            source.payload.market_snapshot_checksum=serp.binding.payload_checksum
            source.binding.payload_checksum=request_digest(source.payload)
    return reseal(m,q)


def test_confirmed_query_fixture_generates_v14_with_exact_same_collected_words_and_zero_provider_calls():
    from app.report_v22.capability_public import build_public_capability_report
    m,q=confirmed_input();before=canonical_json_bytes(q)
    with patch('httpx.Client.send',side_effect=AssertionError('NO_COLLECTION')) as sync,patch('httpx.AsyncClient.send',side_effect=AssertionError('NO_COLLECTION')) as async_:
        report=build_public_capability_report(m,q)
    sync.assert_not_called();async_.assert_not_called()
    assert report.schema_version=='2.2.9'
    assert m.keyword_queries==CONFIRMED_QUERIES
    assert [row.query for row in report.keyword_visibility.rows]==CONFIRMED_QUERIES
    assert next(s for s in q.evidence_input.sources if s.kind=='serp').payload.queries==CONFIRMED_QUERIES
    assert canonical_json_bytes(q)==before


@pytest.mark.parametrize('kind',['short','long','blank','duplicate','casefold','digest','revision'])
def test_binding_rejects_invalid_confirmed_words_or_source_revision(kind):
    queries=list(CONFIRMED_QUERIES)
    if kind=='short':queries=queries[:2]
    if kind=='long':queries[0]='x'*301
    if kind=='blank':queries[0]=''
    if kind=='duplicate':queries[0]=queries[1].upper()
    if kind=='casefold':queries[:2]=['Straße','STRASSE']
    value=dict(queries=queries,queries_digest=verified_request_digest(queries),planning_revision=2)
    if kind=='digest':value['queries_digest']='sha256:'+'0'*64
    if kind=='revision':value['planning_revision']=0
    with pytest.raises(ValueError):TargetKeywordBinding(**value)


def test_old_envelope_stays_byte_identical_and_new_envelope_requires_exact_queries():
    from test_v22_capability_acquisition_preparation import inputs
    _,old,_=inputs();raw=old.model_dump(mode='json')
    assert 'target_keywords' not in raw and raw['schema_version']=='v22_analysis_request_envelope_v1'
    assert canonical_json_bytes(AnalysisRequestEnvelope.model_validate_json(canonical_json_bytes(raw)))==canonical_json_bytes(raw)
    value=deepcopy(raw);value['schema_version']='v22_analysis_request_envelope_v2'
    value['target_keywords']=TargetKeywordBinding(queries=old.analyze_request.queries,queries_digest=verified_request_digest(old.analyze_request.queries),planning_revision=2).model_dump(mode='json')
    assert AnalysisRequestEnvelope.model_validate_json(canonical_json_bytes(value)).target_keywords.planning_revision==2
    value['analyze_request']['queries']=list(reversed(old.analyze_request.queries))
    with pytest.raises(ValueError,match='TARGET_KEYWORDS_DISCOVERY_CONFLICT'):AnalysisRequestEnvelope.model_validate_json(canonical_json_bytes(value))


@pytest.fixture
def anyio_backend():return 'asyncio'

@pytest.mark.anyio
async def test_keyword_intake_freezes_confirmed_revision_digest_and_replay_uses_same_envelope(monkeypatch):
    from test_v22_capability_acquisition_intake import scenario,enable
    from test_api_v2_jobs import build_app
    from app.core.config import settings
    app,runtime,_,_=build_app(monkeypatch);args,envelope,saved,status,headers=scenario()
    binding=TargetKeywordBinding(queries=envelope.analyze_request.queries,queries_digest=verified_request_digest(envelope.analyze_request.queries),planning_revision=2)
    saved['request']={**saved['request'],'schema_version':'v22_analysis_request_envelope_v2','target_keywords':binding.model_dump(mode='json')}
    recorded=None;calls=[]
    def respond(sent):
        nonlocal recorded
        name=sent.url.path.rsplit('/',1)[-1];body=json.loads(sent.content);calls.append(name)
        if name=='lookup_v22_acquisition_intake':return httpx.Response(200,content=json.dumps(recorded))
        if name=='start_v22_acquisition_intake_with_planning':
            assert json.loads(body['p_request_canonical'])==saved['request'];recorded=saved;return httpx.Response(200,json=saved)
        assert name=='get_v22_acquisition_status';return httpx.Response(200,json=status)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as rpc:
        enable(monkeypatch,runtime,rpc,envelope);monkeypatch.setattr(settings,'V22_TARGET_KEYWORDS_ENABLED',True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
            h={**headers,'X-SearchTrust-Planning-Revision':'3','X-SearchTrust-Keyword-Planning-Revision':'2','X-SearchTrust-Keyword-Digest':binding.queries_digest}
            for _ in range(2):
                result=await client.post('/api/v2/capability-acquisition',headers=h,json=saved['request']['analyze_request']);assert result.status_code==202,result.text
            bad=await client.post('/api/v2/capability-acquisition',headers={**h,'X-SearchTrust-Keyword-Digest':'sha256:'+'0'*64},json=saved['request']['analyze_request'])
            assert bad.status_code==409 and bad.json()['detail']['code']=='TARGET_KEYWORDS_DISCOVERY_CONFLICT'
    assert calls.count('start_v22_acquisition_intake_with_planning')==1
    assert runtime.discovery_verifier.verify.await_count==1
    await runtime.redis.aclose()


def test_suggestions_use_only_matched_gbp_and_explicit_title_h1_service_phrases():
    from types import SimpleNamespace as N
    from app.preflight_v22.keyword_suggestions import saved_keyword_suggestions
    signals=N(phones=[N(value='5125550100')],services=[N(value='Emergency plumbing')])
    profile=lambda name,url,phone:N(categories=(name,),website_url=url,phone=phone,public_gbp_url='https://maps.google.com/?cid=1')
    gbp=N(candidates=[profile('Plumber','https://example.test/',None),profile('Other company','https://elsewhere.test/','9995550100')])
    result=saved_keyword_suggestions('<title>Emergency plumbing | Example</title><h1>Emergency Plumbing</h1><h1>Home</h1>',signals,gbp,'https://example.test/')
    assert [i['source'] for i in result['items']]==['matched_gbp_category','site_title','site_h1']
    assert [i['text'] for i in result['items']]==['Plumber','Emergency plumbing','Emergency Plumbing']
    assert not any(i['text']=='Other company' for i in result['items'])

@pytest.mark.anyio
async def test_suggestions_do_not_add_preflight_calls_or_change_disabled_bytes_and_reuse_old_cache(monkeypatch):
    from uuid import UUID
    from app.core.config import settings
    from app.preflight_v22.service import PreflightService
    from test_v22_preflight_service import FakeFetcher,FakeGbpLookup,MemoryCache,snapshot,gbp_found,request_payload
    monkeypatch.setattr('app.preflight_v22.service.uuid4',lambda:UUID(int=42))
    results=[]
    for enabled in (False,True):
        monkeypatch.setattr(settings,'V22_TARGET_KEYWORDS_ENABLED',enabled)
        fetcher=FakeFetcher(snapshot());gbp=FakeGbpLookup(gbp_found())
        service=PreflightService(fetcher=fetcher,gbp_lookup=gbp,cache=MemoryCache(),serpapi_configured=True,pagespeed_configured=False)
        results.append(await service.run(request_payload()))
        assert fetcher.calls==1 and gbp.calls==1
    old=results[0].model_dump(mode='json');new=results[1].model_dump(mode='json')
    assert 'target_keyword_suggestions' not in old
    assert new.pop('target_keyword_suggestions')['items']
    assert canonical_json_bytes(new)==canonical_json_bytes(old)
    fetcher=FakeFetcher(AssertionError('must reuse cached response'));gbp=FakeGbpLookup(gbp_found())
    cached=PreflightService(fetcher=fetcher,gbp_lookup=gbp,cache=MemoryCache(results[0]),serpapi_configured=True,pagespeed_configured=False)
    assert await cached.run(request_payload())==results[0]
    assert fetcher.calls==gbp.calls==0

@pytest.mark.anyio
async def test_disabled_worker_rejects_new_binding_before_any_collection(monkeypatch):
    from types import SimpleNamespace
    from test_v22_capability_acquisition_preparation import inputs
    from app.jobs_v22.capability_acquisition_preparation import CapabilityAcquisitionPreparer,AcquisitionPreparation
    from app.jobs_v22.errors import DeterministicJobError
    args,envelope,raw=inputs()
    binding=TargetKeywordBinding(queries=envelope.analyze_request.queries,queries_digest=verified_request_digest(envelope.analyze_request.queries),planning_revision=1)
    envelope=envelope.model_copy(update={'schema_version':'v22_analysis_request_envelope_v2','target_keywords':binding})
    receipt=AcquisitionPreparation.model_validate_json(canonical_json_bytes({**raw,'request_checksum':verified_request_digest(envelope.model_dump(mode='json'))}))
    begin=AsyncMock(return_value=(receipt,envelope));monkeypatch.setattr('app.jobs_v22.capability_acquisition_preparation.begin_acquisition_preparation',begin)
    collector=SimpleNamespace(collect_sources=AsyncMock())
    prep=CapabilityAcquisitionPreparer(repository=SimpleNamespace(),collector=collector,discovery_verifier=SimpleNamespace(verify=AsyncMock(return_value=envelope.competitor_discovery)),clock=lambda:args['now'],target_keywords_enabled=False)
    with pytest.raises(DeterministicJobError,match='TARGET_KEYWORDS_UNAVAILABLE'):
        await prep.prepare(user_id=args['user_id'],job_id=args['job_id'],idempotency_key=receipt.idempotency_key,business_version_id=args['business'].id,request=envelope,checkpoints=SimpleNamespace(run_generation=1),cost_ledger=SimpleNamespace(job_id=args['job_id'],job_created_at=args['created_at']))
    collector.collect_sources.assert_not_awaited()


def test_confirmed_word_list_is_the_authoritative_manifest_input_and_mismatch_never_rewrites_evidence():
    from app.report_v22.keyword_visibility import prepare_keyword_input
    m,q=confirmed_input();before=canonical_json_bytes(q)
    bound,_=prepare_keyword_input(m,q,confirmed_queries=list(CONFIRMED_QUERIES));assert bound.keyword_queries==CONFIRMED_QUERIES
    with pytest.raises(ValueError,match='TARGET_KEYWORDS_DISCOVERY_CONFLICT'):
        prepare_keyword_input(m,q,confirmed_queries=list(reversed(CONFIRMED_QUERIES)))
    assert canonical_json_bytes(q)==before
