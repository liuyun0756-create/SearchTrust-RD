"""Continuous local review rounds. Original reports and approved plans stay pinned."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from uuid import UUID,uuid4

import pytest
from app.jobs_v22.digest import canonical_json_bytes,verified_request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.review_plan_basis import PlanBasisReport,review_plan_basis
from app.report_v22.review_freeze_models import ReviewFreezeSelection
from app.report_v22.page_check_storage import PageCheckRepository,PageCheckSelection
from tests.integration.test_native_review_intake import intake,decisions,review_setup,prepared,native_database
from tests.test_v22_review_decisions import request,item
from tests.integration.test_native_page_check_storage import page_setup

pytestmark=[pytest.mark.native_integration,pytest.mark.anyio,pytest.mark.timeout(120)]

async def completed(p):
    receipt=await p['service'].run(p['command']);job=receipt.job_id;token=uuid4()
    assert await p['work'].dispatch(job,token)
    await p['coordinator'].execute(job,token)
    return await p['reports'].read(user_id=receipt.user_id,case_id=receipt.case_id,job_id=job)

async def approve_and_record(p,r,*,long=False):
    owner=dict(user_id=str(r.business.user_id),case_id=str(r.business.case_id),report_id=str(r.id))
    body=dict(schema_version='v22_review_delivery_revision_v1',**owner,expected_revision=0,source_checksum=verified_request_digest(r.model_dump(mode='json')),status='approved',title='Agreed next plan',summary='Agreed work for the next review.',client_note=None,actions=[a.model_dump(mode='json') for a in r.actions])
    if long and body['actions']:body['actions'][0]['steps']=['Approved work detail. '*170]
    d=await p['db'].rpc('save_v22_review_delivery',{'p_'+k:v for k,v in owner.items()}|dict(p_request_text=json.dumps(dict(id=str(uuid4()),review_confirmed=True,content=body))))
    entries=[dict(action_id=a['id'],scope=a['target'],implemented_on=datetime.now(timezone.utc).strftime('%Y-%m-%d'),timezone='UTC',note='This round only.',responsible=None) for a in body['actions']]
    record=dict(schema_version='v22_implementation_record_v1',id=str(uuid4()),**owner,report_checksum=body['source_checksum'],delivery_revision_id=d['id'],delivery_revision=d['revision'],revision=1,recorded_at=datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),entries=entries)
    record['checksum']=verified_request_digest(record)
    await p['db'].rpc('save_v22_implementation_record',{'p_'+k:v for k,v in owner.items()}|dict(p_delivery_revision_id=d['id'],p_expected_revision=0,p_payload=record,p_canonical_content=canonical_json_bytes({k:v for k,v in record.items() if k!='checksum'}).decode()))
    choice=ReviewFreezeSelection.model_validate_json(json.dumps(dict(report=dict(id=str(r.id),checksum=body['source_checksum']),delivery=dict(id=d['id'],revision=d['revision'],checksum=verified_request_digest(body)),implementation=dict(id=record['id'],revision=1,checksum=record['checksum']),actions=[dict(action_id=a['id'],page_current_id=None,page_previous_id=None,gsc_id=None,ga4_id=None) for a in body['actions']])))
    return owner,d,record,choice

async def select_next(p,r,choice,*,kind='continue'):
    p['reviews'].allow_multi_round=True
    saved=await p['reviews'].freeze(user_id=r.business.user_id,case_id=r.business.case_id,review_id=uuid4(),selection=choice)
    assert isinstance(saved.bundle.report,PlanBasisReport)
    assert saved.bundle.report==review_plan_basis(r)
    raw=[] if not saved.bundle.delivery.actions else [item(saved,kind)]
    d=await p['decision_service'].save(user_id=r.business.user_id,case_id=r.business.case_id,report_id=r.id,request=request(saved,raw))
    p['service'].preparer.allow_multi_round=True
    p['command'].update(report_id=str(r.id),selection=dict(job_id=str(uuid4()),review_id=str(saved.id),review_checksum=saved.bundle.checksum,decision_id=str(d.id),decision_checksum=d.checksum))
    return saved,d

async def balance(p):
    return (await p['db'].query('select credit_balance from users where id=%s',[p['owner']['user_id']]))[0]['credit_balance']

async def test_native_three_rounds_preserve_approved_work_lineage_and_single_credit(intake):
    p=intake;start=await balance(p);r=await completed(p);original=r.model_dump_json();sizes=[]
    for index in range(3):
        before=await balance(p);owner,d,record,choice=await approve_and_record(p,r,long=index==0)
        saved,decision=await select_next(p,r,choice)
        assert await balance(p)==before
        if index==0:assert len(saved.bundle.delivery.actions[0].steps[0])>2000
        previous=r;r=await completed(p)
        assert r.version==previous.version+1 and r.input_reference.report_id==previous.id
        assert r.content.prior_diagnosis==previous.content.prior_diagnosis
        assert r.actions[0].steps==saved.bundle.delivery.actions[0].steps
        assert len(r.content.seo_actions)==len(previous.actions)
        assert r.content.seo_actions[0].facts.page is None # no copied old observation
        sizes.append(len(r.model_dump_json()))
        assert (await p['reports'].read(user_id=previous.business.user_id,case_id=previous.business.case_id,job_id=previous.id)).model_dump_json()==previous.model_dump_json()
    assert start-await balance(p)==4 and max(sizes)-min(sizes)<1000
    rows=await p['db'].query("select c.state,count(*) as n from workflow_charges c join capability_review_generation_requests r on r.job_id=c.analysis_job_id group by c.state")
    assert rows==[dict(state='consumed',n=4)]
    # Old completed result remains exact and recoverable after later rounds.
    assert original

async def test_native_multi_round_zero_actions_and_disabled_basis(intake):
    p=intake
    d=await p['decision_service'].save(**p['owner'],request=request(p['evidence'],[],previous_id=str(p['decision'].id),previous_checksum=p['decision'].checksum))
    p['command']['selection'].update(decision_id=str(d.id),decision_checksum=d.checksum)
    r=await completed(p);assert r.actions==[]
    owner,d,record,choice=await approve_and_record(p,r)
    with pytest.raises(DeterministicJobError):await p['reviews'].resolve(user_id=r.business.user_id,case_id=r.business.case_id,selection=choice)
    saved,decision=await select_next(p,r,choice)
    assert saved.bundle.selections==[] and decision.decisions==[]
    r2=await completed(p);assert r2.actions==[] and r2.version==r.version+1

async def test_native_multi_round_rejects_cross_round_observation_and_checks_page_source(intake,page_setup):
    from tests.integration.test_native_review_freeze import page_request,page_execute
    p=intake
    prior=await page_request(p);prior=await page_execute(p,prior)
    r=await completed(p);owner,d,record,choice=await approve_and_record(p,r)
    p['reviews'].allow_multi_round=True
    old=prior.id
    if old:
        wrong=choice.model_dump(mode='json');wrong['actions'][0]['page_current_id']=str(old)
        with pytest.raises(DeterministicJobError):await p['reviews'].resolve(user_id=r.business.user_id,case_id=r.business.case_id,selection=wrong)
    repo=PageCheckRepository(url='https://local-test.invalid',service_role_key='local',http_client=p['work'].client)
    selection=PageCheckSelection.model_validate_json(json.dumps(dict(implementation=choice.implementation.model_dump(mode='json'),action_id=record['entries'][0]['action_id'],target_url=r.business.site_url.rstrip('/')+'/',conditions=[dict(id='scope',kind='text_present',value='Repair')])) )
    plan,_,_=await repo.resolve(user_id=r.business.user_id,case_id=r.business.case_id,selection=selection)
    assert plan.binding.report_id==r.id and str(plan.binding.delivery_revision_id)==d['id']
    from app.report_v22.capability import CapabilityReport
    with pytest.raises(ValueError):CapabilityReport.model_validate_json(r.model_dump_json())
    legacy=(await p['db'].query('select * from get_v22_completed_capability_report(%s,%s,%s)',[r.business.user_id,r.business.case_id,r.id],True))[0]
    assert legacy['payload']['schema_version']=='2.2.3' # unchanged stored report, never a fake legacy report


async def test_native_later_round_real_arq_and_source_tampering(intake,arq_pool,redis_prefix):
    from arq.worker import Worker,func
    from app.jobs_v22.review_worker import reconcile_v22_review_generations,execute_v22_review_generation
    from tests.integration.test_native_review_freeze import raw_args
    from app.report_v22.review_generation_inputs import ReviewGenerationPreparer
    p=intake;r=await completed(p);owner,d,record,choice=await approve_and_record(p,r)
    p['reviews'].allow_multi_round=True
    resolved=await p['reviews'].resolve(user_id=r.business.user_id,case_id=r.business.case_id,selection=choice)
    body=resolved.bundle.model_dump(mode='json');body['report']['actions'][0]['target']='Forged target'
    body['report']['checksum']=verified_request_digest({k:v for k,v in body['report'].items() if k!='checksum'})
    with pytest.raises(Exception,match='REVIEW_INVALID'):
        await p['db'].rpc('freeze_v22_review_evidence',raw_args(p,resolved,body=body,selection=choice))
    saved,decision=await select_next(p,r,choice)
    blocked=ReviewGenerationPreparer(p['decision_service'])
    with pytest.raises(DeterministicJobError):await blocked.prepare(**owner,selection=p['command']['selection'])
    before=await balance(p);receipt=await p['service'].run(p['command'])
    ctx=dict(review_generation_coordinator=p['coordinator'],redis=arq_pool,review_generation_queue=redis_prefix+':multi')
    await reconcile_v22_review_generations(ctx)
    worker=Worker([func(execute_v22_review_generation,max_tries=1,keep_result=0)],redis_pool=arq_pool,queue_name=ctx['review_generation_queue'],ctx=ctx,burst=True,handle_signals=False,poll_delay=.01)
    try:await worker.async_run()
    finally:await worker.close()
    final=await p['reports'].read(user_id=receipt.user_id,case_id=receipt.case_id,job_id=receipt.job_id)
    assert final.version==r.version+1 and await balance(p)==before-1

async def test_native_current_round_page_check_is_saved_as_new_observation(intake,page_setup):
    from tests.integration.test_native_page_check_storage import execute as page_execute
    p=intake;r=await completed(p);owner,d,record,choice=await approve_and_record(p,r)
    selection=PageCheckSelection.model_validate_json(json.dumps(dict(implementation=choice.implementation.model_dump(mode='json'),action_id=record['entries'][0]['action_id'],target_url=r.business.site_url.rstrip('/')+'/',conditions=[dict(id='scope',kind='text_present',value='Repair')])) )
    job=await p['pages'].request_check(user_id=r.business.user_id,case_id=r.business.case_id,check_id=uuid4(),selection=selection,report_id=r.id,delivery_revision_id=UUID(d['id']))
    done=await page_execute(p,job);assert done.status=='completed'
    picked=choice.model_dump(mode='json');picked['actions'][0]['page_current_id']=str(done.id)
    choice=ReviewFreezeSelection.model_validate_json(json.dumps(picked))
    saved,_=await select_next(p,r,choice)
    assert saved.bundle.selections[0].page.current.plan.binding.report_id==r.id
    next_report=await completed(p)
    facts=next_report.content.seo_actions[0].facts.page
    assert facts is not None and facts.comparison=='first_observation' and facts.previous_observed_at is None
    assert facts.observed_at==done.observation.observed_at
