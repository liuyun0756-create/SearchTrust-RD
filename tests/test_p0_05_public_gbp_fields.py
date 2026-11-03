"""Versioned place details, actual published response shape and frozen compatibility."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path

import fakeredis.aioredis
import httpx
import pytest
from pydantic import TypeAdapter, ValidationError

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.customer_public_gbp_stage import (
    CHECKPOINT_VERSION, LEGACY_STAGE_VERSION, STAGE_VERSION,
    CheckpointedCustomerPublicGbpStage, CustomerPublicGbpCollection,
    SerpApiCustomerPublicGbpProvider, _reference, _sanitize,
)
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.public_gbp_details import detail_fields
from app.report_v22.public_gbp_models import (
    CustomerPublicGbpSnapshotValue, EXTENDED_FIELD_NAMES, FIELD_NAMES,
)
from app.report_v22.public_gbp_snapshot import build_customer_public_gbp_snapshot
from public_gbp_helpers import sample_input
from test_v22_customer_public_gbp_stage import JOB_ID, NOW, ledger, payload, request

FIXTURES = Path(__file__).parent / 'fixtures/p0_05'


def v2_snapshot(details=None):
    raw = sample_input()
    raw['record']['fields'].update(detail_fields(details or {}))
    return build_customer_public_gbp_snapshot(raw, extended_fields=True)


def test_published_place_response_fields_and_whitelist():
    raw = json.loads((FIXTURES / 'serpapi-place-anonymized.json').read_text())
    before = deepcopy(raw)
    raw['place_results'].update(user_reviews={'most_relevant':[{'text':'do not retain'}]}, images=[{'title':'image'}])
    clean = _sanitize(raw)
    assert 'user_reviews' not in clean['place_results'] and 'images' not in clean['place_results']
    fields = detail_fields(clean['place_results'])
    for key, expected in [('categories', before['place_results']['type']), ('rating',4.5), ('review_count',1388), ('hours',before['place_results']['hours'])]:
        assert fields[key] == {'state':'observed','value':expected}
    assert fields['photo_count'] == fields['operating_hours'] == {'state':'not_returned','value':None}
    assert v2_snapshot(clean['place_results']).schema_version == 'customer_public_gbp_snapshot_v2'


@pytest.mark.parametrize('field', EXTENDED_FIELD_NAMES)
def test_absent_fields_are_not_returned_never_zero(field):
    decoded = detail_fields(_sanitize(payload())['place_results'])[field]
    assert decoded == {'state':'not_returned','value':[] if field=='categories' else None}


@pytest.mark.parametrize('provider_key,field', [('type','categories'), ('types','categories'), ('rating','rating'),
    ('reviews','review_count'), ('hours','hours'), ('operating_hours','operating_hours'), ('photos_count','photo_count'), ('photo_count','photo_count')])
@pytest.mark.parametrize('empty', [None, '', [], {}])
def test_explicit_empty_is_not_absent(provider_key,field,empty):
    raw=payload(); raw['place_results'][provider_key]=empty
    assert detail_fields(_sanitize(raw)['place_results'])[field]['state'] == 'returned_empty'


@pytest.mark.parametrize('key,field', [('rating','rating'),('reviews','review_count'),('photos_count','photo_count')])
def test_observed_zero_is_preserved(key,field):
    assert detail_fields({key:0})[field] == {'state':'observed','value':0}


@pytest.mark.parametrize('raw,field', [({'rating':True},'rating'),({'rating':float('nan')},'rating'),
    ({'rating':6},'rating'),({'reviews':-1},'review_count'),({'reviews':'1388'},'review_count'),
    ({'reviews':[{'text':'not a count'}]},'review_count'),({'photos_count':False},'photo_count'),
    ({'photos_count':1,'photo_count':2},'photo_count'),({'types':{'name':'Coffee shop'}},'categories'),
    ({'type':[' ']},'categories'),({'hours':{'holiday':'Open'}},'hours'),
    ({'operating_hours':{'monday':' '}},'operating_hours')])
def test_unknown_shapes_are_unsupported_not_missing(raw,field):
    decoded=detail_fields(raw)
    assert decoded[field] == {'state':'unsupported','value':[] if field=='categories' else None}
    assert v2_snapshot(raw).health_status == 'healthy'


def test_aliases_preserve_schedules_and_never_count_gallery_images():
    raw={'type':'Coffee shop','types':['Coffee shop','Cafe'], 'hours':[{'monday':'9AM–5PM'}],
         'operating_hours':{'monday':'10AM–4PM'}, 'images':[{},{}], 'photos_link':'https://example.test/photos'}
    fields=detail_fields(raw)
    assert fields['categories']['value'] == ['Coffee shop','Cafe']
    for key in ('hours','operating_hours'): assert fields[key]['value'] == raw[key]
    assert fields['photo_count']['state'] == 'not_returned'
    assert detail_fields({'photo_count':12})['photo_count']['value'] == 12


def test_failed_sample_requires_request_failed_for_all_v2_fields():
    raw=sample_input()
    raw['record']['fields'].update(detail_fields({}))
    for key,value in raw['record']['fields'].items():
        value.update(state='request_failed', value=[] if key in ('categories','service_areas') else None)
    raw.update(collection_status='failed',failure_code='timeout',response_checksum=None)
    raw['record'].update(observed_entity_keys=[],observed_public_gbp_url=None)
    result=build_customer_public_gbp_snapshot(raw,extended_fields=True)
    assert result.health_status == 'unavailable'
    assert all(getattr(result.record.fields,key).state=='request_failed' for key in (*FIELD_NAMES,*EXTENDED_FIELD_NAMES))
    changed=result.model_dump(mode='json'); changed['record']['fields']['rating']['state']='not_returned'
    with pytest.raises(ValidationError): TypeAdapter(CustomerPublicGbpSnapshotValue).validate_json(json.dumps(changed))


def test_one_place_call_and_v2_checkpoint_restart_zero_additional_calls():
    async def run():
        raw=json.loads((FIXTURES / 'serpapi-place-anonymized.json').read_text())
        calls=[]
        def handler(req):
            calls.append(req)
            return httpx.Response(200,json=raw)
        redis=fakeredis.aioredis.FakeRedis()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=SerpApiCustomerPublicGbpProvider(keys=['fixture'],base_url='https://serpapi.example/search',
                connect_timeout=1,read_timeout=1,total_timeout=2,max_response_bytes=100_000,client_factory=lambda:client)
            stage=CheckpointedCustomerPublicGbpStage(provider,clock=lambda:NOW)
            cp=JobCheckpoints(redis,prefix='p005',ttl_seconds=3600)
            args=dict(job_id=JOB_ID,request=request(),submitted_at=NOW-timedelta(minutes=1),checkpoints=cp,cost_ledger=ledger(redis))
            first=await stage.collect(**args)
            second=await stage.collect(**args)
            assert canonical_json_bytes(first)==canonical_json_bytes(second)
            assert first.schema_version==CHECKPOINT_VERSION and first.snapshot.schema_version.endswith('_v2')
            assert first.snapshot.record.fields.review_count.value==1388
            assert len(calls)==1
            assert dict(calls[0].url.params)=={'engine':'google_maps','type':'place','hl':'en','data_cid':'123','api_key':'fixture'}
            claims=(await ledger(redis).state()).claims
            assert len(claims)==1 and claims[0].operation=='serpapi_customer_public_gbp'
        await redis.aclose()
    asyncio.run(run())


def test_v1_checkpoint_reuse_preserves_bytes_and_makes_no_provider_calls():
    async def run():
        redis=fakeredis.aioredis.FakeRedis()
        cp=JobCheckpoints(redis,prefix='p005',ttl_seconds=3600)
        reference=_reference(request(),NOW-timedelta(minutes=1))
        raw=sample_input()
        raw.update(started_at=NOW,completed_at=NOW,expires_at=NOW+timedelta(days=1),reference=reference,request_target={'public_gbp_url':reference.public_gbp_url,'entity_keys':reference.entity_keys})
        raw['record']['observed_entity_keys']=reference.entity_keys
        raw['record']['observed_public_gbp_url']=reference.public_gbp_url
        old=CustomerPublicGbpCollection(reference=reference,snapshot=build_customer_public_gbp_snapshot(raw))
        digest=request_digest(reference)
        saved={'schema_version':'customer_public_gbp_checkpoint_v1','stage_version':LEGACY_STAGE_VERSION,
            'job_id':str(JOB_ID),'input_digest':digest,'result_checksum':request_digest(old),'result':old.model_dump(mode='json')}
        key=f'{LEGACY_STAGE_VERSION}:result:{digest[7:]}'
        await cp.save(JOB_ID,key,saved)
        class NoCalls:
            async def request(self,*args,**kwargs): raise AssertionError('must reuse frozen checkpoint')
        result=await CheckpointedCustomerPublicGbpStage(NoCalls(),clock=lambda:pytest.fail('clock accessed')).collect(
            job_id=JOB_ID,request=request(),submitted_at=reference.confirmed_at,checkpoints=cp)
        assert canonical_json_bytes(result)==canonical_json_bytes(old)
        assert canonical_json_bytes(await cp.get(JOB_ID,key))==canonical_json_bytes(saved)
        assert await cp.get(JOB_ID,f'{STAGE_VERSION}:result:{digest[7:]}') is None
        assert set(type(result.snapshot.record.fields).model_fields)==set(FIELD_NAMES)
        await redis.aclose()
    asyncio.run(run())


def report_fixture(extended=True):
    from app.jobs_v22.digest import verified_request_digest
    from app.report_v22.capability import AnalysisInputManifest
    from app.report_v22.capability_public import build_public_capability_report
    from app.report_v22.evidence_models import PublicGbpEvidenceSource
    from test_v22_capability_public import inputs
    manifest,value=inputs([{'status_code':503}])
    if extended:
        old=next(s for s in value.evidence_input.sources if s.kind=='public_gbp')
        details=json.loads((FIXTURES / 'serpapi-place-anonymized.json').read_text())['place_results']
        snapshot=v2_snapshot(details)
        binding=old.binding.model_copy(update={'schema_version':snapshot.schema_version,'payload_checksum':request_digest(snapshot)})
        value.evidence_input.sources[-1]=PublicGbpEvidenceSource(binding=binding,payload=snapshot)
        raw=manifest.model_dump(mode='json')
        raw['snapshots'][-1].update(schema_version=snapshot.schema_version,payload_checksum=request_digest(snapshot))
        raw['generation_input_checksum']=verified_request_digest(value.model_dump(mode='json'))
        raw['input_checksum']=verified_request_digest({k:v for k,v in raw.items() if k!='input_checksum'})
        manifest=AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw))
    return manifest,value,build_public_capability_report(manifest,value)


@pytest.mark.parametrize('extended',[False,True])
def test_snapshot_versions_generate_reports_and_replay_without_new_claims(extended):
    from app.jobs_v22.capability_inputs import generation_recipe
    from app.report_v22.capability_public import build_public_capability_report
    from app.report_v22.findings_models import PublicFindingsInput
    manifest,value,report=report_fixture(extended)
    before=canonical_json_bytes(value)
    restored=PublicFindingsInput.model_validate_json(before)
    assert canonical_json_bytes(restored)==before
    assert canonical_json_bytes(build_public_capability_report(manifest,restored))==canonical_json_bytes(report)
    assert generation_recipe(manifest,value)[2]['schema_version']=='v22_generation_recipe_v1'
    public=next(s for s in restored.evidence_input.sources if s.kind=='public_gbp')
    assert public.payload.schema_version.endswith('_v2' if extended else '_v1')
    assert ('review_count' in public.payload.record.fields.model_dump()) is extended
    old=report_fixture(False)[2]
    assert [f.rule_id for f in report.findings]==[f.rule_id for f in old.findings]
    assert [a.title for a in report.actions]==[a.title for a in old.actions]
    assert not any(e.field_path.endswith(('/rating','/review_count','/photo_count','/hours')) for e in report.evidence)


def test_live_v2_snapshot_crosses_acquisition_bridge_and_verified_reader():
    from dataclasses import replace
    from app.jobs_v22.capability_acquisition_input import build_acquisition_input
    from app.jobs_v22.verified_models import VerifiedPublicGbpSnapshotRow
    from app.report_v22.capability_public import build_public_capability_report
    from test_v22_capability_acquisition_preparation import inputs
    args,_,_=inputs()
    original=args['collected'].customer_public_gbp
    snapshot=v2_snapshot({'rating':4.5,'reviews':1388})
    args['collected']=replace(args['collected'],customer_public_gbp=CustomerPublicGbpCollection(
        schema_version=CHECKPOINT_VERSION,reference=original.reference,snapshot=snapshot))
    manifest,value=build_acquisition_input(**args)
    assert manifest.snapshots[3].schema_version==snapshot.schema_version
    assert build_public_capability_report(manifest,value).schema_version=='2.2.2'
    row=VerifiedPublicGbpSnapshotRow(snapshot_id=JOB_ID,case_id=original.reference.case_id,source_type='gbp',
        schema_version=snapshot.schema_version,normalized_payload=snapshot,payload_checksum=request_digest(snapshot),
        created_at=snapshot.completed_at,fetched_at=snapshot.completed_at,expires_at=snapshot.expires_at,reference=original.reference)
    restored=VerifiedPublicGbpSnapshotRow.model_validate_json(canonical_json_bytes(row))
    assert canonical_json_bytes(row)==canonical_json_bytes(restored)
    wrong=row.model_dump(mode='json'); wrong['schema_version']='customer_public_gbp_snapshot_v1'
    with pytest.raises(ValidationError): VerifiedPublicGbpSnapshotRow.model_validate_json(json.dumps(wrong))
