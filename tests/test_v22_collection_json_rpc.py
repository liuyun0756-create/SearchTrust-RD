"""Opt-in real SQL RPC round trip in a disposable local Supabase database.

Run with SEARCHTRUST_NUL_RPC_TEST=1 and -m native_integration. No provider IO,
credentials, linked projects, or production database URLs are used.
"""
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

import httpx
from pydantic import BaseModel
import pytest

from app.jobs_v22.collection_json import normalize_collection_json
from app.jobs_v22.capability_acquisition_input import build_acquisition_input
from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository, require_frozen_capability_input
from app.jobs_v22.capability_runtime_models import CapabilityTaskRequest
from app.jobs_v22.digest import request_digest, verified_canonical_json_bytes
from test_v22_capability_acquisition_preparation import inputs

pytestmark = pytest.mark.native_integration

@pytest.fixture
def anyio_backend():
    return 'asyncio'


def quoted(value):
    if value is None:
        return 'null'
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture
def local_database():
    if os.environ.get('SEARCHTRUST_NUL_RPC_TEST') != '1':
        pytest.skip('Real NUL RPC proof requires explicit SEARCHTRUST_NUL_RPC_TEST=1 and local Supabase Docker; SQL is not checked')
    migrations = Path(__file__).resolve().parents[2] / 'search-trust/supabase/migrations'
    if not migrations.is_dir():
        pytest.skip('Real SQL RPC proof requires sibling search-trust migrations; SQL is not checked')
    # Fixed local container, generated database identifier. Never accept a URL.
    name = 'searchtrust_nul_test_' + uuid4().hex
    base = ['docker', 'exec', '-i', 'supabase_db_supabase', 'psql', '-X', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1', '-At']
    def execute(sql, database=name):
        result = subprocess.run([*base, '-d', database], input=sql, capture_output=True, text=True, timeout=120)
        if result.returncode:
            # Test inputs are synthetic; don't dump SQL parameters or payloads.
            raise AssertionError('\n'.join(line for line in result.stderr.splitlines() if line.startswith(('ERROR:', 'DETAIL:'))))
        return result.stdout.strip()
    execute('create database ' + name, 'postgres')
    try:
        # Role settings are cluster-wide, so exercise business migrations here;
        # the already-tested timeout-only migration must not reconfigure a stack.
        for migration in sorted(migrations.glob('*.sql')):
            if migration.name == '20261110130000_set_service_role_statement_timeout.sql':
                continue
            execute(migration.read_text())
        yield execute
    finally:
        execute('drop database ' + name + ' with (force)', 'postgres')


def fresh_inputs(source):
    args, envelope, _ = inputs()
    delta = datetime.now(timezone.utc) - timedelta(seconds=2) - args['now']
    def shift(value):
        if isinstance(value, datetime):
            return value + delta
        if isinstance(value, str) and re.match(r'^\d{4}-\d\d-\d\dT', value):
            return (datetime.fromisoformat(value.replace('Z', '+00:00')) + delta).isoformat().replace('+00:00', 'Z')
        if isinstance(value, BaseModel):
            for key in type(value).model_fields:
                setattr(value, key, shift(getattr(value, key)))
        elif isinstance(value, dict):
            return {k: shift(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [shift(v) for v in value]
        elif is_dataclass(value):
            for field in fields(value):
                object.__setattr__(value, field.name, shift(getattr(value, field.name)))
        return value
    args = shift(args)
    c = args['collected']
    if source == 'site':
        raw = c.site_inventory.model_dump(mode='json')
        raw['limitations'].append('synthetic\0site')
        original = c.site_inventory
    elif source == 'serp':
        raw = c.shared_market.snapshot.model_dump(mode='json')
        raw['query_runs'][0]['results'][0]['display_name'] += '\0'
        original = c.shared_market.snapshot
    elif source == 'competitor':
        raw = c.competitor_collection.model_dump(mode='json')
        raw['competitors'][0]['limitations'].append('synthetic\0competitor')
        original = c.competitor_collection
    elif source == 'public_gbp':
        raw = c.customer_public_gbp.snapshot.model_dump(mode='json')
        raw['record']['fields']['business_name']['value'] += '\0'
        original = c.customer_public_gbp.snapshot
    else:
        from app.collectors.site_render_models import RenderedSiteSnapshot, RenderedPage
        original = RenderedSiteSnapshot(root_url=c.site_inventory.root_url, static_payload_checksum=request_digest(c.site_inventory),
            started_at=args['now'], completed_at=args['now'], pages=[RenderedPage(url=c.site_inventory.root_url,
                reason='static_not_checked', static_text_characters=0, limitation='render_unavailable')], limitations=['synthetic\0rendered'])
        raw = original.model_dump(mode='json')
    raw['schema_\0version'] = raw.pop('schema_version')
    normalized = normalize_collection_json(raw, source=source, job_id=args['job_id'])
    model = type(original).model_validate_json(json.dumps(normalized))
    if source == 'site':
        object.__setattr__(c, 'site_inventory', model)
    elif source == 'serp':
        c.shared_market.snapshot = model
    elif source == 'competitor':
        object.__setattr__(c, 'competitor_collection', model)
    elif source == 'public_gbp':
        c.customer_public_gbp.snapshot = model
    else:
        args['rendered'] = model
    # These are new synthetic observations, not edits to saved evidence.
    c.shared_market.snapshot_checksum = request_digest(c.shared_market.snapshot)
    c.discovery.market_snapshot_checksum = c.shared_market.snapshot_checksum
    c.competitor_collection.market_snapshot_checksum = c.shared_market.snapshot_checksum
    c.customer_public_gbp.snapshot.subject_reference_checksum = request_digest(c.customer_public_gbp.reference)
    envelope.competitor_discovery.market_snapshot_checksum = c.shared_market.snapshot_checksum
    return args, envelope


@pytest.mark.anyio
@pytest.mark.parametrize('source', ['site', 'serp', 'competitor', 'public_gbp', 'rendered'])
async def test_each_source_real_freeze_and_repository_resolve(local_database, source):
    execute = local_database
    args, envelope = fresh_inputs(source)
    manifest, value = build_acquisition_input(**args)
    m = manifest.model_dump(mode='json')
    q = envelope.analyze_request.model_dump(mode='json')
    owner, case, job = m['user_id'], m['case_id'], m['job_id']
    lease = str(uuid4())
    execute(f"""
      insert into users(id,clerk_user_id,email,credit_balance) values({quoted(owner)},{quoted(owner)},'nul-test@example.invalid',1);
      insert into client_cases(id,user_id,site_url,normalized_domain,business_name,business_identity,operating_model,primary_service,target_market)
      values({quoted(case)},{quoted(owner)},{quoted(q['business_identity']['site_url'])},{quoted(q['business_identity']['normalized_domain'])},{quoted(q['business_identity']['business_name'])},
        {quoted(q['business_identity'])}::jsonb,{quoted(q['business_identity']['operating_model'])},{quoted(q['primary_service'])},{quoted(q['target_market'])}::jsonb);
      select save_v22_business_scope({quoted(owner)},{quoted(case)},0,{quoted(m['business'])}::jsonb);
      insert into analysis_jobs(id,case_id,job_type,status,current_stage,idempotency_key,created_at,deadline_at)
      values({quoted(job)},{quoted(case)},'prospect_report','queued','queued','nul-test',{quoted(args['created_at'].isoformat())},{quoted(args['deadline_at'].isoformat())});
      insert into workflow_charges(id,user_id,case_id,workflow_kind,idempotency_key,analysis_job_id,discovery_job_id)
      values({quoted(job)},{quoted(owner)},{quoted(case)},'prospect','nul-test',{quoted(job)},gen_random_uuid());
      insert into credit_ledger(user_id,case_id,workflow_charge_id,analysis_job_id,kind,delta,balance_after,idempotency_key)
      values({quoted(owner)},{quoted(case)},{quoted(job)},{quoted(job)},'prospect_debit',-1,1,{quoted(job)});
      select begin_v22_acquisition_preparation({quoted(owner)},{quoted(case)},{quoted(job)},'nul-test',{quoted(m['business']['id'])},
        {quoted(verified_canonical_json_bytes(envelope.model_dump(mode='json')).decode())});
      select claim_v22_acquisition_preparation({quoted(job)},1,{quoted(lease)},3);
    """)
    calls = []
    async def transport(request):
        body = json.loads(request.content)
        rpc = request.url.path.rsplit('/', 1)[-1]
        calls.append(rpc)
        assert request.url.host == 'local-test.invalid'
        if rpc == 'prepare_v22_capability_analysis_input':
            assert '\0' not in body['p_generation_canonical']
            sql = f"select freeze_v22_acquisition_preparation({quoted(lease)},'{{\"cost_schema_version\":1,\"cost_ledger_revision\":3}}'::jsonb,{quoted(body)}::jsonb)"
        else:
            assert rpc == 'resolve_v22_capability_analysis_input'
            sql = 'select resolve_v22_capability_analysis_input(' + ','.join(k + '=>' + quoted(v) for k, v in body.items()) + ')'
        result = execute('set role service_role; ' + sql)
        return httpx.Response(200, json=json.loads(result.splitlines()[-1]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        repository = SupabaseCapabilityInputRepository(url='https://local-test.invalid', service_role_key='synthetic-local-only', http_client=client)
        await repository.prepare(manifest=manifest, request=value, run_generation=1)
        resolved = await repository.resolve(job_id=args['job_id'], run_generation=1, request=CapabilityTaskRequest(
            case_id=manifest.case_id, manifest_id=manifest.id, input_checksum=manifest.input_checksum, report_type='prospect'))
        require_frozen_capability_input(resolved, run_generation=1)
        assert resolved.request == value
        assert resolved.manifest == manifest
        assert all(s.binding.payload_checksum == request_digest(s.payload) for s in resolved.request.evidence_input.sources)
    assert calls == ['prepare_v22_capability_analysis_input', 'resolve_v22_capability_analysis_input']
