"""No external I/O: command, API, and uncertain acknowledgement boundaries."""
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from app.api.v2 import review_generation as api
from app.core.config import settings
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.review_intake import ReviewIntakeService, ReviewJob, parse_command
from app.jobs_v22.review_worker import ReviewCoordinator, reconcile_v22_review_generations

pytestmark=pytest.mark.anyio

@pytest.fixture
def anyio_backend(): return "asyncio"


def command():
    return dict(operation='start',user_id=str(uuid4()),case_id=str(uuid4()),report_id=str(uuid4()),confirmed=True,
        selection=dict(job_id=str(uuid4()),review_id=str(uuid4()),review_checksum='sha256:'+'a'*64,decision_id=str(uuid4()),decision_checksum='sha256:'+'b'*64))


def receipt(c):
    return ReviewJob.model_validate_json(json.dumps(dict(schema_version='v22_review_job_v1',job_id=c['selection']['job_id'],user_id=c['user_id'],case_id=c['case_id'],base_report_id=c['report_id'],
        selection=c['selection'],input_checksum='sha256:'+'c'*64,revision=1,run_generation=1,status='queued',created_at='2026-10-27T00:00:00Z',deadline_at='2026-10-27T00:20:00Z',updated_at='2026-10-27T00:00:00Z',report_id=None,credit_state='reserved',error_code=None)))

@pytest.mark.parametrize('kind',['extra','false','integer','no-confirm','nested','private','duplicate','oversize','unknown'])
async def test_command_rejects_unconfirmed_or_unbounded_input(kind):
    c=command()
    if kind=='extra':c['raw_source']='private'
    if kind=='false':c['confirmed']=False
    if kind=='integer':c['confirmed']=1
    if kind=='no-confirm':c.pop('confirmed')
    if kind=='nested':c['selection']['extra']='bad'
    if kind=='private':c['selection']['input_text']='bad'
    if kind=='unknown':c['operation']='retry'
    if kind=='duplicate':c='{"operation":"start","operation":"status"}'
    if kind=='oversize':c['payload']='x'*9000
    with pytest.raises(DeterministicJobError):parse_command(c)

async def test_idempotent_lookup_precedes_source_preparation_and_no_new_debit():
    c=command();r=receipt(c);repo=SimpleNamespace(run=AsyncMock(return_value=r));preparer=SimpleNamespace(prepare=AsyncMock())
    assert await ReviewIntakeService(repo,preparer).run(c)==r
    assert repo.run.call_args.args[0]=='lookup_v22_review_generation'
    preparer.prepare.assert_not_called()

async def test_ambiguous_lookup_never_creates_a_replacement():
    repo=SimpleNamespace(run=AsyncMock(side_effect=TransientJobError('UNKNOWN','unknown')));preparer=SimpleNamespace(prepare=AsyncMock())
    with pytest.raises(TransientJobError):await ReviewIntakeService(repo,preparer).run(command())
    preparer.prepare.assert_not_called();assert repo.run.await_count==1

async def test_queue_loss_leaves_sql_lease_for_recovery():
    job=uuid4();work=SimpleNamespace(pending=AsyncMock(return_value=[job]),dispatch=AsyncMock(return_value=True))
    redis=SimpleNamespace(enqueue_job=AsyncMock(side_effect=ConnectionError('offline')))
    await reconcile_v22_review_generations(dict(review_generation_coordinator=SimpleNamespace(work=work),redis=redis,review_generation_queue='isolated'))
    redis.enqueue_job.assert_awaited_once();assert redis.enqueue_job.call_args.args[1]==str(job)
    # Disabled context is inert, including no storage calls.
    await reconcile_v22_review_generations({});assert work.pending.await_count==1

async def test_api_flag_auth_size_and_safe_receipt(monkeypatch):
    app=FastAPI();app.include_router(api.router);c=command();mock=SimpleNamespace(run=AsyncMock(return_value=receipt(c)))
    async def service():yield mock
    app.dependency_overrides[api.service]=service
    monkeypatch.setattr(settings,'V22_INTERNAL_API_TOKEN',SecretStr('local-test-token'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.post('/api/v2/review-generation',json=c)).status_code==401
        headers={'Authorization':'Bearer local-test-token'}
        monkeypatch.setattr(settings,'V22_REVIEW_GENERATION_ENABLED',False)
        assert (await client.post('/api/v2/review-generation',json=c,headers=headers)).status_code==404
        for k in ('V22_REVIEW_GENERATION_ENABLED','V22_REVIEW_DECISIONS_WORKSPACE_ENABLED','V22_REVIEW_EVIDENCE_WORKSPACE_ENABLED','V22_CAPABILITY_EXECUTION_ENABLED'):monkeypatch.setattr(settings,k,True)
        result=await client.post('/api/v2/review-generation',json=c,headers=headers);assert result.status_code==200 and result.json()['credit_state']=='reserved'
        assert result.headers['cache-control']=='private, no-store'
        assert (await client.post('/api/v2/review-generation',content=b'x'*9000,headers=headers|{'Content-Type':'application/json'})).status_code==413
        assert (await client.post('/api/v2/review-generation',json=c|{'confirmed':False},headers=headers)).status_code==400
        mock.run.side_effect=TransientJobError('UNKNOWN','private SQL details')
        result=await client.post('/api/v2/review-generation',json=c,headers=headers);assert result.status_code==503 and result.json()=={'code':'UNAVAILABLE'}
        mock.run.side_effect=DeterministicJobError('V22_REVIEW_START_INSUFFICIENT_CREDITS','private')
        assert (await client.post('/api/v2/review-generation',json=c,headers=headers)).status_code==402
