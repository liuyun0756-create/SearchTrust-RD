"""The 19367a12 input shape, recreated with synthetic data and no providers."""
from collections import Counter
from dataclasses import dataclass, replace
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import HttpUrl

from app.collectors.site_inventory_models import InventoryCount, SiteInventorySnapshotV3
from app.jobs_v22.acquisition_input_budget import bound_customer_html, SIZE_LIMIT_NOTE
from app.jobs_v22.capability_acquisition_input import build_acquisition_input
from app.jobs_v22.capability_acquisition_preparation import CapabilityAcquisitionPreparer
from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository, MAX_GENERATION_CANONICAL_BYTES
from app.jobs_v22.cost_models import ALLOWED_COUNTER_KEYS
from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.competitors_v22.selection import analysis_discovery_input_digest, AnalysisRequestEnvelope
from app.jobs_v22.target_keywords import TargetKeywordBinding
from app.report_v22.capability import AnalysisInputManifest
from app.report_v22.site_business_facts import build_site_business_facts
from app.report_v22.models import TargetMarket
from site_business_helpers import two_pages, source as deep_source
from test_v22_capability_acquisition_preparation import inputs, dispatch_for


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@dataclass(frozen=True)
class Sources:
    site_inventory: object
    other: object = None


def v3(payload):
    return SiteInventorySnapshotV3.model_validate_json(canonical_json_bytes({**payload.model_dump(mode='json'), 'schema_version':'site_inventory_snapshot_v3'}))


def test_projection_keeps_original_fetch_history_and_marks_omitted_fields_unknown():
    request=two_pages('<p><a href="tel:+15125550100">Call</a></p>')
    original=Sources(v3(request.source.payload),other=object())
    before=canonical_json_bytes(original.site_inventory)
    first=original.site_inventory.selected_pages[0].deep_snapshot
    limited=bound_customer_html(original,max_html_bytes=len(first.html.encode()))
    assert limited is not original and limited.other is original.other
    assert canonical_json_bytes(original.site_inventory)==before
    assert limited.site_inventory.pages==original.site_inventory.pages
    assert limited.site_inventory.deep_attempts==original.site_inventory.deep_attempts
    assert limited.site_inventory.selected_pages[0].deep_snapshot==first
    assert limited.site_inventory.selected_pages[1].deep_snapshot is None
    assert 'resource_limit' in limited.site_inventory.selected_pages[1].selection_reasons
    assert limited.site_inventory.deep_analyzed_count==1
    assert SIZE_LIMIT_NOTE in limited.site_inventory.limitations
    request.source.payload=limited.site_inventory
    request.source.binding.payload_checksum=request_digest(limited.site_inventory)
    request.source.binding.schema_version=limited.site_inventory.schema_version
    result=build_site_business_facts(request,resource_limits=True)
    assert result.pages[1].status=='not_checked'
    assert result.pages[1].diagnostics[0].code=='resource_limit'
    assert all(f.observation_status=='not_checked' for f in result.pages[1].fields.values())


def test_byte_budget_is_utf8_and_noop_preserves_the_exact_snapshot():
    original=Sources(v3(two_pages('<p>中文</p>').source.payload))
    first=original.site_inventory.selected_pages[0].deep_snapshot.html
    assert len(first.encode())>len(first)
    assert bound_customer_html(original,max_html_bytes=2*len(first.encode())) is original
    limited=bound_customer_html(original,max_html_bytes=2*len(first))
    assert limited.site_inventory.deep_analyzed_count<2
    assert canonical_json_bytes(bound_customer_html(original,max_html_bytes=len(first.encode())).site_inventory)==canonical_json_bytes(bound_customer_html(original,max_html_bytes=len(first.encode())).site_inventory)


def test_historical_v1_snapshots_are_not_rewritten():
    original=Sources(two_pages().source.payload)
    assert bound_customer_html(original,max_html_bytes=1) is original


def test_retained_html_whitespace_and_checksum_are_not_normalized():
    site=v3(two_pages('<p>saved</p>').source.payload)
    first=site.selected_pages[0].deep_snapshot
    first.html=' \n'+first.html+'\n '
    first.content_checksum='sha256:'+hashlib.sha256(first.html.encode()).hexdigest()
    original=Sources(site)
    before=canonical_json_bytes(first)
    limited=bound_customer_html(original,max_html_bytes=len(first.html.encode()))
    assert canonical_json_bytes(limited.site_inventory.selected_pages[0].deep_snapshot)==before
    assert limited.site_inventory.selected_pages[1].deep_snapshot is None


def test_page_order_and_prefix_exhaustion_are_deterministic():
    site=v3(two_pages('<p>one</p>').source.payload)
    selected=site.selected_pages[0].model_copy(deep=True)
    url=HttpUrl('https://example.test/last')
    selected.url=selected.deep_snapshot.url=url
    site.pages.append(site.pages[0].model_copy(update={'url':url}))
    site.selected_pages.append(selected)
    site.discovered_url_count=site.structurally_checked_count=site.deep_analyzed_count=3
    site.page_type_counts[0].count=site.source_counts[0].count=3
    site.selected_pages[1].deep_snapshot.html='<p>'+('x'*100)+'</p>'
    site.selected_pages=list(reversed(site.selected_pages))
    original=Sources(v3(site))
    budget=len(selected.deep_snapshot.html.encode())+1
    limited=bound_customer_html(original,max_html_bytes=budget).site_inventory
    kept={str(p.url) for p in limited.selected_pages if p.deep_analyzed}
    assert kept=={str(site.pages[0].url)}  # Do not fit a later small page after exhaustion.


def large_tour_inputs():
    args,envelope,receipt=inputs()
    old=args['collected'];request=envelope.analyze_request.model_copy(deep=True)
    old_queries=request.queries[:]
    request.primary_service='Tour operator'
    request.target_market=TargetMarket(display_name='Keaau, HI, US',country_code='US',city='Keaau',region='HI',
        latitude=request.target_market.latitude,longitude=request.target_market.longitude)
    request.business_identity.primary_location=request.target_market
    request.queries=['Tour operator Keaau','Tour operator near me','best Tour operator Keaau']
    replacements=dict(zip(old_queries,request.queries))
    def change(value):
        if isinstance(value,dict):return {k:change(v) for k,v in value.items()}
        if isinstance(value,list):return [change(v) for v in value]
        return replacements.get(value,value) if isinstance(value,str) else value
    serp=type(old.shared_market.snapshot).model_validate_json(json.dumps(change(old.shared_market.snapshot.model_dump(mode='json'))))
    serp.target_point.requested_label=request.target_market.display_name
    shared=old.shared_market.model_copy(update={'snapshot':serp,'snapshot_checksum':request_digest(serp),'input_digest':analysis_discovery_input_digest(request)})
    discovery=old.discovery.model_copy(update={'input_digest':shared.input_digest,'market_snapshot_checksum':shared.snapshot_checksum})
    rivals=old.competitor_collection.model_copy(update={'market_snapshot_checksum':shared.snapshot_checksum})
    template=deep_source('<html><body><h1>Tour operator</h1></body></html>').payload
    html='<html><body><h1>Tour operator</h1>'+(' '*1_500_000)+'</body></html>'
    pages=[];selected=[];attempts=[]
    for i in range(46):
        url=HttpUrl('https://example.test/'+(f'tours/{i}/' if i else ''))
        kind='home' if i==0 else 'service_detail'
        pages.append(template.pages[0].model_copy(update={'url':url,'final_url':url,'canonical_url':url,'page_type':kind}))
        deep=template.selected_pages[0].deep_snapshot.model_copy(update={'url':url,'final_url':url,'page_type':kind,'html':html,
            'text':'','response_bytes':len(html.encode()),'content_checksum':'sha256:'+hashlib.sha256(html.encode()).hexdigest()})
        selected.append(template.selected_pages[0].model_copy(update={'url':url,'page_type':kind,'deep_snapshot':deep}))
        # Preserve real fetch-success metadata even if a derived input omits HTML.
        from app.collectors.site_inventory_models import DeepPageAttempt
        attempts.append(DeepPageAttempt(url=url,rank=i+1,succeeded=True,selected_final=True))
    site=SiteInventorySnapshotV3.model_validate_json(canonical_json_bytes({**template.model_dump(mode='json'),'schema_version':'site_inventory_snapshot_v3',
        'pages':[p.model_dump(mode='json') for p in pages],'selected_pages':[p.model_dump(mode='json') for p in selected],'deep_attempts':[p.model_dump(mode='json') for p in attempts],'discovery_limit':500,'deep_analysis_limit':50,
        'discovered_url_count':46,'structurally_checked_count':46,'deep_analyzed_count':46,
        'page_type_counts':[InventoryCount(label=k,count=v).model_dump(mode='json') for k,v in Counter(p.page_type for p in pages).items()],
        'source_counts':[InventoryCount(label='seed',count=46).model_dump(mode='json')],'limitations':['firecrawl_unavailable']}))
    collected=replace(old,site_inventory=site,shared_market=shared,discovery=discovery,competitor_collection=rivals)
    business=args['business'].model_copy(update={'primary_service':request.primary_service,'target_market':request.target_market.display_name})
    binding=TargetKeywordBinding(queries=request.queries,queries_digest=verified_request_digest(request.queries),planning_revision=1)
    link=envelope.competitor_discovery.model_copy(update={'market_snapshot_checksum':shared.snapshot_checksum})
    envelope=AnalysisRequestEnvelope(schema_version='v22_analysis_request_envelope_v2',analyze_request=request,competitor_discovery=link,target_keywords=binding)
    costs={k:0 for k in ALLOWED_COUNTER_KEYS};costs.update(cost_schema_version=1,cost_ledger_revision=1,pricing_revision=1,
        firecrawl_attempts=3,firecrawl_failures=3,provider_attempts_total=3,provider_failures_total=3)
    receipt={**receipt,'business':business.model_dump(mode='json'),'request_checksum':verified_request_digest(envelope.model_dump(mode='json')),'cost_counters':costs}
    args={**args,'business':business,'request':request,'collected':collected}
    return args,envelope,receipt


@pytest.mark.anyio
async def test_46_page_tour_operator_with_three_failed_firecrawl_maps_prepares_without_external_calls():
    args,envelope,receipt=large_tour_inputs();original=args['collected'];before=canonical_json_bytes(original.site_inventory)
    manifest,value=build_acquisition_input(**args)
    from app.report_v22.keyword_visibility import prepare_keyword_input
    manifest,value=prepare_keyword_input(manifest,value,confirmed_queries=envelope.target_keywords.queries)
    assert len(verified_canonical_json_bytes(value.model_dump(mode='json')))>MAX_GENERATION_CANONICAL_BYTES
    direct=object.__new__(SupabaseCapabilityInputRepository);direct.rpc=SimpleNamespace(post=AsyncMock())
    with pytest.raises(DeterministicJobError,match='^V22_CAPABILITY_INPUT_INVALID$'):
        await direct.prepare(manifest=manifest,request=value,run_generation=1)
    direct.rpc.post.assert_not_called()
    calls=[];prepared={};rendered_static=[]
    def respond(sent):
        body=json.loads(sent.content);calls.append(sent.url.path)
        if sent.url.path.endswith('begin_v22_acquisition_preparation'):return httpx.Response(200,json=receipt)
        if sent.url.path.endswith('prepare_v22_capability_analysis_input'):
            prepared.update(body);bound=AnalysisInputManifest.model_validate_json(json.dumps(body['p_manifest']))
            return httpx.Response(200,json={'manifest_id':str(bound.id),'input_checksum':bound.input_checksum,'generation_input_checksum':bound.generation_input_checksum})
        return httpx.Response(200,json=dispatch_for(args,AnalysisInputManifest.model_validate_json(json.dumps(prepared['p_manifest']))))
    async def render(**kwargs):rendered_static.append(kwargs['static']);return None
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        repo=SupabaseCapabilityInputRepository(url='https://offline.invalid',service_role_key='fixture',http_client=client)
        collector=SimpleNamespace(collect_sources=AsyncMock(return_value=original))
        prep=CapabilityAcquisitionPreparer(repository=repo,collector=collector,discovery_verifier=SimpleNamespace(verify=AsyncMock(return_value=envelope.competitor_discovery)),
            target_keywords_enabled=True,clock=lambda:args['now'],render_stage=SimpleNamespace(collect=AsyncMock(side_effect=render)))
        ledger=SimpleNamespace(job_id=args['job_id'],job_created_at=args['created_at'],redis=SimpleNamespace(exists=AsyncMock(return_value=True)),key='synthetic-ledger',claim=AsyncMock(side_effect=AssertionError('provider denied')))
        await prep.prepare(user_id=args['user_id'],job_id=args['job_id'],idempotency_key=receipt['idempotency_key'],business_version_id=args['business'].id,
            request=envelope,checkpoints=SimpleNamespace(run_generation=1),cost_ledger=ledger)
    assert len(calls)==3 and collector.collect_sources.call_count==1
    ledger.claim.assert_not_called()
    assert len(prepared['p_generation_canonical'].encode())<=MAX_GENERATION_CANONICAL_BYTES
    generated=json.loads(prepared['p_generation_canonical']);site=next(s for s in generated['evidence_input']['sources'] if s['kind']=='site')
    assert len(site['payload']['pages'])==46 and site['payload']['deep_analyzed_count']<46
    assert any('resource_limit' in p['selection_reasons'] and p['deep_snapshot'] is None for p in site['payload']['selected_pages'])
    assert rendered_static[0].model_dump(mode='json')==site['payload']
    assert receipt['cost_counters']['firecrawl_failures']==3
    assert canonical_json_bytes(original.site_inventory)==before
    assert prepared['p_manifest']['ruleset_version']=='v22_capability_public_v14'
    assert hashlib.sha256(prepared['p_generation_canonical'].encode()).hexdigest()==prepared['p_manifest']['generation_input_checksum'][7:]
    for source in generated['evidence_input']['sources']:
        assert hashlib.sha256(prepared['p_public_source_canonicals'][source['kind']].encode()).hexdigest()==source['binding']['payload_checksum'][7:]
