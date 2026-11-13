"""Real Python intake/preparer/repository + the frontend's full SQL migration chain.

This local cross-repository integration requires the migration workspace and its
installed PGlite dependency. A standalone backend checkout reports that missing
integration dependency explicitly; it never substitutes mocked SQL success.
"""
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel
import pytest

from app.jobs_v22.capability_acquisition_intake import submit_acquisition
from app.jobs_v22.capability_acquisition_preparation import CapabilityAcquisitionPreparer
from app.jobs_v22.capability_acquisition_worker import LeasedPreparationRpc
from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository
from app.jobs_v22.cost_models import ALLOWED_COUNTER_KEYS
from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_request_digest
from app.jobs_v22.target_keywords import TargetKeywordBinding
from test_v22_capability_acquisition_preparation import inputs


class SqlError(RuntimeError):
    def __init__(self, value):
        super().__init__(value['error'])
        self.code = value['code']


class LocalSql:
    def __init__(self, workspace):
        self.process = subprocess.Popen(['node', 'scripts/test-v22-preparation-rpc.mjs'],
            cwd=workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        self.calls = []

    def command(self, body):
        self.process.stdin.write(json.dumps(body) + '\n')
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        assert line, self.process.stderr.read() if self.process.poll() is not None else 'SQL bridge returned no result'
        value = json.loads(line)
        if 'error' in value:
            raise SqlError(value)
        return value['result']

    async def post(self, name, body):
        self.calls.append((name, body))
        return self.command(dict(op='rpc', name=name, body=body))

    def invalid(self):
        return AssertionError('Real SQL returned an invalid repository receipt')

    def close(self):
        try:
            self.command(dict(op='close'))
        finally:
            self.process.stdin.close()
            self.process.wait(timeout=10)
            for stream in (self.process.stdout, self.process.stderr):
                stream.close()


def compressed_collection(value, *, old_start, old_end, created):
    """Retimestamp synthetic copies into the new SQL job, retaining source order."""
    def shift(v):
        if isinstance(v, datetime):
            if v > old_end:
                return created + timedelta(minutes=5)
            return created + timedelta(microseconds=int((v-old_start)/(old_end-old_start)*1000))
        if isinstance(v, str) and re.match(r'^\d{4}-\d\d-\d\dT', v):
            return shift(datetime.fromisoformat(v.replace('Z', '+00:00'))).isoformat()
        if isinstance(v, BaseModel):
            return v.model_copy(update={k:shift(getattr(v,k)) for k in type(v).model_fields}, deep=True)
        if is_dataclass(v):
            return replace(v, **{f.name:shift(getattr(v,f.name)) for f in fields(v)})
        if isinstance(v, dict):
            return {k:shift(x) for k,x in v.items()}
        if isinstance(v, list):
            return [shift(x) for x in v]
        return v
    result = shift(value)
    result.shared_market.snapshot_checksum = request_digest(result.shared_market.snapshot)
    result.competitor_collection.market_snapshot_checksum = result.shared_market.snapshot_checksum
    if hasattr(result.customer_public_gbp.snapshot, 'subject_reference_checksum'):
        result.customer_public_gbp.snapshot.subject_reference_checksum = request_digest(result.customer_public_gbp.reference)
    return result


@pytest.mark.anyio
@pytest.mark.parametrize('anyio_backend', ['asyncio'])
@pytest.mark.parametrize('include_fix', [False, True], ids=['production-baseline', 'identity-fix'])
async def test_enabled_intake_prepare_and_real_sql_freeze(monkeypatch, include_fix):
    workspace = Path(os.environ.get('SEARCHTRUST_SQL_WORKSPACE', Path(__file__).resolve().parents[2]/'search-trust'))
    if not shutil.which('node') or not (workspace/'node_modules/@electric-sql/pglite').exists():
        pytest.skip('Cross-repository SQL integration requires Node and the frontend migration workspace with PGlite installed; set SEARCHTRUST_SQL_WORKSPACE. No SQL assertion was substituted.')
    from app.core.config import settings
    monkeypatch.setattr(settings, 'V22_TARGET_KEYWORDS_ENABLED', True)
    args, envelope, _ = inputs()
    req = envelope.analyze_request
    owner, job, workflow = args['user_id'], args['job_id'], uuid4()
    found = envelope.competitor_discovery
    binding = TargetKeywordBinding(queries=req.queries, queries_digest=verified_request_digest(req.queries), planning_revision=1)
    bridge = LocalSql(workspace)
    try:
        bridge.command(dict(op='init',includeFix=include_fix,seed=dict(owner=str(owner),caseId=str(req.case_id),
            identity=req.business_identity.model_dump(mode='json'),service=req.primary_service,market=req.target_market.model_dump(mode='json'))))
        planning = dict(schema_version='v22_planning_settings_v2',priority=None,target_pages=[],
            execution_conditions=dict(capacity='unknown',publishing_access='unknown'),
            target_keywords=[dict(query=q,origin='seo',suggestion_refs=[]) for q in req.queries],
            keyword_scope=dict(primary_service=req.primary_service,target_market=req.target_market.model_dump(mode='json')),keyword_confirmation=None)
        await bridge.post('save_v22_case_planning_settings',dict(p_user_id=str(owner),p_case_id=str(req.case_id),p_expected_revision=0,p_settings=planning))
        await bridge.post('start_v22_prospect_discovery_with_keywords',dict(p_user_id=str(owner),p_case_id=str(req.case_id),p_workflow_id=str(workflow),
            p_discovery_job_id=str(found.discovery_id),p_workflow_idempotency_key='sql-workflow',p_task_idempotency_key='sql-discovery',
            p_planning_revision=1,p_queries_canonical=canonical_json_bytes(req.queries).decode(),p_keyword_scope=planning['keyword_scope']))
        verifier=SimpleNamespace(verify=AsyncMock(return_value=found));queue=SimpleNamespace(enqueue=AsyncMock())
        repository=SupabaseCapabilityInputRepository(url='https://unused.example',service_role_key='synthetic-only',http_client=None)
        repository.rpc=bridge
        status=await submit_acquisition(repository=repository,verifier=verifier,queue=queue,user_id=owner,job_id=job,workflow_id=workflow,
            discovery_id=found.discovery_id,idempotency_key='sql-analysis',previous_job_id=None,request=req,now=datetime.now(timezone.utc),planning_revision=1,target_keywords=binding)
        assert status.job_id==job
        token=uuid4()
        claimed=await bridge.post('claim_v22_acquisition_preparation',dict(p_job_id=str(job),p_run_generation=1,p_lease_token=str(token),p_max_attempts=3))
        assert claimed['outcome']=='claimed'
        receipt=claimed['preparation'];created=datetime.fromisoformat(receipt['created_at'])
        collected=compressed_collection(args['collected'],old_start=args['created_at'],old_end=args['now'],created=created)
        collector=SimpleNamespace(collect_sources=AsyncMock(return_value=collected))
        async def before_freeze():
            return {**dict.fromkeys(ALLOWED_COUNTER_KEYS,0),'cost_schema_version':1,'cost_ledger_revision':3}
        repository.rpc=LeasedPreparationRpc(bridge,token=token,before_freeze=before_freeze)
        preparer=CapabilityAcquisitionPreparer(repository=repository,collector=collector,discovery_verifier=verifier,target_keywords_enabled=settings.V22_TARGET_KEYWORDS_ENABLED)
        call=dict(user_id=owner,job_id=job,idempotency_key='sql-analysis',business_version_id=UUID(receipt['business']['id']),
            request={**envelope.model_dump(mode='json'),'schema_version':'v22_analysis_request_envelope_v2','target_keywords':binding.model_dump(mode='json')},
            checkpoints=SimpleNamespace(run_generation=1),cost_ledger=SimpleNamespace(job_id=job,job_created_at=created))
        # All repository SQL is real. No transport can issue a provider call.
        def no_http(*_args,**_kwargs):raise AssertionError('NO_PROVIDER_CALL_ALLOWED')
        monkeypatch.setattr(httpx.AsyncClient,'send',no_http)
        if not include_fix:
            with pytest.raises(SqlError,match='CAPABILITY_PREPARATION_CONFLICT') as failure:
                await preparer.prepare(**call)
            assert failure.value.code=='P0001'
            assert bridge.command(dict(op='inspect'))['manifests']==[]
        else:
            dispatch=await preparer.prepare(**call)
            state=bridge.command(dict(op='inspect'));manifest=state['manifests'][0]['payload']
            assert manifest['ruleset_version']=='v22_capability_public_v14'
            assert manifest['keyword_queries']==req.queries
            assert state['recipes'][0]['recipe']['request']['business_identity']['schema_version']=='business_identity_v2'
            assert 'schema_version' not in state['preparations'][0]['request']['analyze_request']['business_identity']
            assert dispatch.request.capability_request.manifest_id.hex==manifest['id'].replace('-','')
            assert state['credits']==[dict(credit_balance=2)] and len(state['ledger'])==1
        assert collector.collect_sources.await_count==1
        assert sum(name=='freeze_v22_acquisition_preparation' for name,_ in bridge.calls)==1
    finally:
        bridge.close()
