"""Real local SQL intake + dispatch/worker/completion. No provider/account calls."""
import asyncio
from copy import deepcopy
import json
from uuid import UUID, uuid4

import httpx
import pytest
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.review_intake import ReviewIntakeRepository, ReviewIntakeService, parse_command
from app.jobs_v22.review_input_repository import ReviewInputRepository
from app.jobs_v22.review_report_persistence import ReviewReportRepository
from app.jobs_v22.review_worker import ReviewWorkRepository, ReviewCoordinator, reconcile_v22_review_generations, execute_v22_review_generation
from app.report_v22.review_generation_inputs import ReviewGenerationPreparer
from tests.integration.test_native_review_decisions import decisions, save
from tests.integration.test_native_review_freeze import review_setup, prepared, native_database
from tests.test_v22_review_decisions import request

pytestmark=[pytest.mark.native_integration,pytest.mark.anyio,pytest.mark.timeout(80)]

@pytest.fixture
async def intake(decisions):
    p=decisions;d=await save(p,request(p['evidence']));p['decision']=d
    p['command']=dict(operation='start',**{k:str(v) for k,v in p['owner'].items()},confirmed=True,
        selection=dict(job_id=str(uuid4()),review_id=str(p['evidence'].id),review_checksum=p['evidence'].bundle.checksum,decision_id=str(d.id),decision_checksum=d.checksum))
    async with httpx.AsyncClient(transport=httpx.MockTransport(p['db'].transport)) as client:
        kw=dict(url='https://local-test.invalid',service_role_key='local',http_client=client)
        p['service']=ReviewIntakeService(ReviewIntakeRepository(**kw),ReviewGenerationPreparer(p['decision_service']))
        p['work']=ReviewWorkRepository(**kw);p['inputs']=ReviewInputRepository(**kw);p['reports']=ReviewReportRepository(**kw)
        p['coordinator']=ReviewCoordinator(work=p['work'],inputs=p['inputs'],reports=p['reports'])
        yield p

async def state(p):
    return await p['db'].query("select credit_balance,(select count(*) from credit_ledger where analysis_job_id=%s) as entries from users where id=%s",[p['command']['selection']['job_id'],p['owner']['user_id']])

async def read(p):
    return await p['service'].run(dict(operation='status',**{k:str(v) for k,v in p['owner'].items()},job_id=p['command']['selection']['job_id']))

async def claim(p):
    job=UUID(p['command']['selection']['job_id']);token=uuid4();run_token=uuid4()
    assert await p['work'].dispatch(job,token)
    return await p['work'].claim(job,token,run_token),run_token

async def test_native_intake_atomic_concurrent_recovery_and_fixed_selection(intake):
    p=intake;before=await state(p)
    receipts=await asyncio.gather(*(p['service'].run(p['command']) for _ in range(4)))
    assert all(r==receipts[0] for r in receipts)
    assert receipts[0].credit_state=='reserved'
    after=await state(p);assert after[0]['credit_balance']==before[0]['credit_balance']-1 and after[0]['entries']==1
    q=deepcopy(p['command']);q['selection']['job_id']=str(uuid4())
    with pytest.raises(DeterministicJobError,match='CONFLICT'):await p['service'].run(q)
    q=deepcopy(p['command']);q['selection']['decision_checksum']='sha256:'+'0'*64
    with pytest.raises(DeterministicJobError,match='CONFLICT'):await p['service'].run(q)
    assert await state(p)==after
    found=await p['service'].run(dict(operation='find',**{k:str(v) for k,v in p['owner'].items()},review_id=p['command']['selection']['review_id'],decision_id=p['command']['selection']['decision_id']))
    assert found==receipts[0]
    assert not {'input_text','recipe','reason','bundle'}.intersection(found.model_dump())

async def test_native_intake_rollback_insufficient_and_source_change(intake):
    p=intake;c=parse_command(p['command']);v=await p['service'].preparer.prepare(**p['owner'],selection=c.selection)
    await p['db'].query('update users set credit_balance=0 where id=%s',[c.user_id])
    with pytest.raises(DeterministicJobError,match='INSUFFICIENT_CREDITS'):await p['service'].run(c)
    assert (await state(p))[0]['entries']==0
    await p['db'].query('update users set credit_balance=5 where id=%s',[c.user_id])
    # A failure after debit/binding rolls every write back.
    await p['db'].query("create function test_review_insert_failure() returns trigger language plpgsql as $$begin raise exception 'TEST_ROLLBACK';end;$$")
    await p['db'].query('create trigger test_review_rollback before insert on capability_review_generation_work for each row execute function test_review_insert_failure()')
    with pytest.raises(DeterministicJobError):await p['service'].run(c)
    assert (await state(p))[0]==dict(credit_balance=5,entries=0)
    assert await p['db'].query('select job_id from capability_review_generation_inputs')==[]
    await p['db'].query('drop trigger test_review_rollback on capability_review_generation_work')
    raw=v.model_dump(mode='json');raw['evidence']['bundle']['report']['title']='Tampered'
    args=dict(p_user_id=str(c.user_id),p_case_id=str(c.case_id),p_report_id=str(c.report_id),p_command_text=c.selection.model_dump_json(),p_input_text=json.dumps(raw))
    with pytest.raises(Exception,match='REVIEW_GENERATION_INVALID'):await p['db'].rpc('start_v22_review_generation',args)
    assert (await state(p))[0]['entries']==0
    assert (await p['service'].run(c)).status=='queued'

async def test_native_dispatch_worker_success_zero_actions_and_no_double_charge(intake):
    p=intake;d=await save(p,request(p['evidence'],[],previous_id=str(p['decision'].id),previous_checksum=p['decision'].checksum))
    p['command']['selection'].update(decision_id=str(d.id),decision_checksum=d.checksum)
    first=await p['service'].run(p['command']);before=await state(p)
    class Queue:
        def __init__(self):self.jobs=[]
        async def enqueue_job(self,*a,**kw):self.jobs.append((a,kw));return True
    queue=Queue();ctx=dict(review_generation_coordinator=p['coordinator'],redis=queue,review_generation_queue='isolated')
    await reconcile_v22_review_generations(ctx);await reconcile_v22_review_generations(ctx)
    assert len(queue.jobs)==1
    a,kw=queue.jobs[0];assert a[0]=='execute_v22_review_generation' and kw['_queue_name']=='isolated'
    await asyncio.gather(*(execute_v22_review_generation(ctx,*a[1:]) for _ in range(2)))
    result=await read(p);assert result.status=='succeeded' and result.credit_state=='consumed' and result.report_id==result.job_id
    r=await p['reports'].read(user_id=result.user_id,case_id=result.case_id,job_id=result.job_id)
    assert r.actions==[] and r.action_state=='no_next_actions'
    assert await state(p)==before
    assert (await p['service'].run(p['command'])).report_id==result.job_id
    assert await p['work'].pending()==[]

async def test_native_lease_takeover_rejects_old_generation_and_lost_completion_ack(intake):
    p=intake;await p['service'].run(p['command']);run,token=await claim(p)
    assert run.run_generation==2 and await p['work'].heartbeat(run,token)
    frozen=await p['inputs'].resolve(user_id=run.user_id,case_id=run.case_id,job_id=run.job_id,run_generation=run.run_generation)
    await p['db'].query("update capability_review_generation_work set run_until=clock_timestamp()-interval '1 second' where job_id=%s",[run.job_id])
    # Consumed dispatch token cannot be used to claim a new execution.
    assert await p['work'].claim(run.job_id,uuid4(),uuid4()) is None
    newer,new_token=await claim(p);assert newer.run_generation==3
    assert not await p['work'].heartbeat(run,token)
    assert await p['work'].finish(run,token,True)=='inactive'
    with pytest.raises(DeterministicJobError,match='INACTIVE'):
        await p['inputs'].resolve(user_id=run.user_id,case_id=run.case_id,job_id=run.job_id,run_generation=run.run_generation)
    frozen=await p['inputs'].resolve(user_id=run.user_id,case_id=run.case_id,job_id=run.job_id,run_generation=newer.run_generation)
    await p['coordinator'].completion.complete(frozen=frozen,run_generation=newer.run_generation)
    # Worker sees timeout after commit: SQL returns success, never a credit.
    assert await p['work'].finish(newer,new_token,True)=='succeeded'
    assert (await state(p))[0]['entries']==1

@pytest.mark.parametrize('kind',['deadline','archive','exhaustion'])
async def test_native_technical_expiry_compensates_once_and_never_after_success(intake,kind):
    p=intake;before=await state(p);await p['service'].run(p['command']);job=p['command']['selection']['job_id']
    if kind=='deadline':await p['db'].query("update analysis_jobs set deadline_at=clock_timestamp()-interval '1 second',created_at=clock_timestamp()-interval '21 minutes' where id=%s",[job])
    elif kind=='archive':await p['db'].query("update client_cases set status='archived',archived_at=clock_timestamp() where id=%s",[p['owner']['case_id']])
    else:await p['db'].query('update analysis_jobs set attempt_count=3 where id=%s',[job])
    await p['work'].pending();await p['work'].pending()
    after=await state(p);assert after[0]['credit_balance']==before[0]['credit_balance'] and after[0]['entries']==2
    rows=await p['db'].query('select status from analysis_jobs where id=%s',[job]);assert rows[0]['status']=='failed'
    if kind!='archive':assert (await read(p)).credit_state=='compensated'

async def test_native_dispatch_loss_permissions_and_retry_after_technical_failure(intake):
    p=intake;await p['service'].run(p['command']);job=UUID(p['command']['selection']['job_id']);t=uuid4()
    assert await p['work'].dispatch(job,t)
    assert not await p['work'].dispatch(job,uuid4())
    await p['db'].query("update capability_review_generation_work set dispatch_until=clock_timestamp()-interval '1 second' where job_id=%s",[job])
    run,token=await claim(p);assert await p['work'].finish(run,token,True)=='failed'
    assert await p['work'].finish(run,token,True)=='failed'
    p['command']['selection']['job_id']=str(uuid4())
    assert (await p['service'].run(p['command'])).status=='queued'
    rights=await p['db'].query("select has_table_privilege('service_role','capability_review_generation_work','UPDATE') a,has_function_privilege('anon','start_v22_review_generation(uuid,uuid,uuid,text,text)','EXECUTE') b")
    assert not any(rights[0].values())


async def test_native_lost_start_response_and_stale_other_owner_gates(intake):
    p=intake;lost=False
    async def transport(req):
        nonlocal lost
        response=await p['db'].transport(req)
        if req.url.path.endswith('start_v22_review_generation') and response.status_code==200 and not lost:
            lost=True;raise httpx.ReadTimeout('lost receipt',request=req)
        return response
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        repo=ReviewIntakeRepository(url='https://local-test.invalid',service_role_key='local',http_client=client)
        service=ReviewIntakeService(repo,p['service'].preparer)
        with pytest.raises(TransientJobError):await service.run(p['command'])
        recovered=await service.run(p['command']);assert recovered.status=='queued'
        assert (await state(p))[0]['entries']==1
        other=p['command']|dict(user_id=str(uuid4()))
        with pytest.raises(DeterministicJobError):await service.run(other)
    run,token=await claim(p)
    frozen=await p['inputs'].resolve(user_id=run.user_id,case_id=run.case_id,job_id=run.job_id,run_generation=run.run_generation)
    await p['coordinator'].completion.complete(frozen=frozen,run_generation=run.run_generation)
    c=deepcopy(p['command']);c['selection']['job_id']=str(uuid4())
    # Same selection is already delivered; a changed saved decision still cannot
    # generate from a superseded base report.
    newer=await save(p,request(p['evidence'],[],previous_id=str(p['decision'].id),previous_checksum=p['decision'].checksum))
    c['selection'].update(decision_id=str(newer.id),decision_checksum=newer.checksum)
    with pytest.raises(DeterministicJobError,match='STALE'):await p['service'].run(c)
    assert (await state(p))[0]['entries']==1

async def test_native_actual_arq_review_execution_and_completion(intake,arq_pool,redis_prefix):
    from arq.worker import Worker,func
    p=intake;await p['service'].run(p['command']);before=await state(p)
    ctx=dict(review_generation_coordinator=p['coordinator'],redis=arq_pool,review_generation_queue=redis_prefix+':review')
    await reconcile_v22_review_generations(ctx)
    worker=Worker([func(execute_v22_review_generation,max_tries=1,keep_result=0)],redis_pool=arq_pool,
        queue_name=ctx['review_generation_queue'],ctx=ctx,burst=True,handle_signals=False,poll_delay=.01)
    try:await worker.async_run()
    finally:await worker.close()
    result=await read(p);assert result.status=='succeeded' and result.credit_state=='consumed'
    assert await state(p)==before
    assert await p['work'].pending()==[]
