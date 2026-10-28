"""Actual SQL completion atomically stores bounded generated-version changes."""
import asyncio
import json
from uuid import uuid4
import pytest
from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.report_difference import build_report_difference,ReportDifference
from app.report_v22.review_generation_models import ReviewGenerationInput
from app.report_v22.review_report import build_verified_review_report
from tests.integration.test_native_review_intake import intake,decisions,review_setup,prepared,native_database,claim
from tests.integration.test_native_multi_round_review import completed,approve_and_record,select_next
pytestmark=[pytest.mark.native_integration,pytest.mark.anyio,pytest.mark.timeout(150)]

async def snapshot(p,r):
 return await p['db'].rpc('get_v22_review_report_difference',dict(p_user_id=str(r.business.user_id),p_case_id=str(r.business.case_id),p_report_id=str(r.id)))
async def input_for(p,r):
 rows=await p['db'].query('select input_text from capability_review_generation_inputs where job_id=%s',[r.id])
 return ReviewGenerationInput.model_validate_json(rows[0]['input_text'])
async def complete_args(p,r):
 j=(await p['db'].query('select run_generation from analysis_jobs where id=%s',[r.id]))[0]
 return dict(p_user_id=str(r.business.user_id),p_case_id=str(r.business.case_id),p_job_id=str(r.id),p_run_generation=j['run_generation'],p_report_text=canonical_json_bytes(r.model_dump(mode='json')).decode())

async def test_native_first_review_difference_exact_recovery_and_single_charge(intake):
 p=intake;r=await completed(p);d=await snapshot(p,r)
 assert ReportDifference.model_validate_json(json.dumps(d))==build_report_difference(await input_for(p,r),r)
 assert d['evidence_basis']=='first_review'
 before=await p['db'].query('select credit_balance from users where id=%s',[r.business.user_id])
 args=await complete_args(p,r)
 results=await asyncio.gather(*(p['db'].rpc('complete_v22_review_analysis',args) for _ in range(3)))
 assert all(x['idempotent'] for x in results) and await snapshot(p,r)==d
 assert await p['db'].query('select credit_balance from users where id=%s',[r.business.user_id])==before
 assert len(await p['db'].query('select report_id from capability_review_report_differences'))==1

async def test_native_later_round_diff_uses_pinned_approved_plan_and_preserves_history(intake):
 p=intake;r=await completed(p);original=await snapshot(p,r)
 for index in range(2):
  owner,delivery,record,choice=await approve_and_record(p,r,long=index==0)
  await select_next(p,r,choice)
  previous=r;r=await completed(p);d=await snapshot(p,r)
  assert d==build_report_difference(await input_for(p,r),r,previous).model_dump(mode='json')
  assert d['basis']['delivery_id']==delivery['id']
  assert all(a['state']=='unchanged' for a in d['work'])
 assert await snapshot(p,(await p['reports'].read(user_id=r.business.user_id,case_id=r.business.case_id,job_id=original['report_id'])))==original
 assert len(await p['db'].query('select report_id from capability_review_report_differences'))==3

async def test_native_difference_insert_failure_rolls_back_report_and_consumption(intake):
 p=intake;await p['service'].run(p['command']);run,token=await claim(p)
 frozen=await p['inputs'].resolve(user_id=run.user_id,case_id=run.case_id,job_id=run.job_id,run_generation=run.run_generation)
 r=build_verified_review_report(frozen.value)
 await p['db'].query("create function test_difference_failure() returns trigger language plpgsql as $$begin raise exception 'TEST_DIFFERENCE_ROLLBACK';end;$$")
 await p['db'].query('create trigger test_difference_rollback before insert on capability_review_report_differences for each row execute function test_difference_failure()')
 with pytest.raises(Exception):await p['reports'].complete(frozen=frozen,report=r,run_generation=run.run_generation)
 assert await p['db'].query('select id from capability_report_versions where id=%s',[r.id])==[]
 assert (await p['db'].query('select status from analysis_jobs where id=%s',[r.id]))[0]['status']!='succeeded'
 assert (await p['db'].query('select state from workflow_charges where analysis_job_id=%s',[r.id]))[0]['state']=='reserved'
 await p['db'].query('drop trigger test_difference_rollback on capability_review_report_differences')
 await p['reports'].complete(frozen=frozen,report=r,run_generation=run.run_generation)
 assert await snapshot(p,r)

async def test_native_difference_access_immutability_and_legacy_null(intake):
 p=intake;r=await completed(p);args=await complete_args(p,r);original=r.model_dump_json()
 for key in ['p_user_id','p_case_id','p_report_id']:
  q=dict(p_user_id=str(r.business.user_id),p_case_id=str(r.business.case_id),p_report_id=str(r.id));q[key]=str(uuid4())
  with pytest.raises(Exception):await p['db'].rpc('get_v22_review_report_difference',q)
 for role in ['anon','authenticated','service_role']:
  assert not (await p['db'].query("select has_table_privilege(%s,'capability_review_report_differences','SELECT') as allowed",[role]))[0]['allowed']
 with pytest.raises(Exception,match='IMMUTABLE'):await p['db'].query('delete from capability_review_report_differences where report_id=%s',[r.id])
 # Simulate an actual terminal report from before this migration: no diff row.
 await p['db'].query('alter table capability_review_report_differences disable trigger v22_report_difference_immutable')
 await p['db'].query('delete from capability_review_report_differences where report_id=%s',[r.id])
 await p['db'].query('alter table capability_review_report_differences enable trigger v22_report_difference_immutable')
 assert await snapshot(p,r) is None
 assert (await p['db'].rpc('complete_v22_review_analysis',args))['idempotent']
 assert await snapshot(p,r) is None
 assert (await p['reports'].read(user_id=r.business.user_id,case_id=r.business.case_id,job_id=r.id)).model_dump_json()==original

async def test_native_resigned_false_difference_is_rejected(intake):
 p=intake;r=await completed(p)
 await p['db'].query('alter table capability_review_report_differences disable trigger v22_report_difference_immutable')
 await p['db'].query("""with fake as (select report_id,jsonb_set(payload,'{work,0,state}','\"updated\"')-'checksum' as body from capability_review_report_differences where report_id=%s),
 signed as(select report_id,body||jsonb_build_object('checksum',v22_review_hash(body)) as payload from fake)
 update capability_review_report_differences d set payload=s.payload,payload_checksum=v22_review_hash(s.payload) from signed s where s.report_id=d.report_id""",[r.id])
 with pytest.raises(Exception,match='REVIEW_DIFFERENCE_INVALID'):await snapshot(p,r)

async def test_native_zero_action_diagnosis_and_next_baseline_keep_difference_empty(intake):
 from tests.test_v22_review_decisions import request
 p=intake
 start=(await p['db'].query('select credit_balance from users where id=%s',[p['owner']['user_id']]))[0]['credit_balance']
 d=await p['decision_service'].save(**p['owner'],request=request(p['evidence'],[],previous_id=str(p['decision'].id),previous_checksum=p['decision'].checksum))
 p['command']['selection'].update(decision_id=str(d.id),decision_checksum=d.checksum)
 r=await completed(p);first=await snapshot(p,r)
 assert all(a['state']=='not_carried_forward' for a in first['work'])
 _,_,_,choice=await approve_and_record(p,r)
 await select_next(p,r,choice);previous=r;r=await completed(p)
 d=await snapshot(p,r)
 assert d['work']==[] and d['evidence']==[] and not d['plan_order_changed']
 assert d==build_report_difference(await input_for(p,r),r,previous).model_dump(mode='json')
 assert (await p['db'].query('select credit_balance from users where id=%s',[r.business.user_id]))[0]['credit_balance']==start-2
