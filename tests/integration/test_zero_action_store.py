"""Explicit local-Supabase replay; never accepts a production connection URL."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

import pytest

from app.jobs_v22.digest import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[2]
CONTAINER = os.getenv('ZERO_ACTION_LOCAL_PG_CONTAINER')
REPORT = os.getenv('ZERO_ACTION_REPLAY_REPORT')
FROZEN = os.getenv('ZERO_ACTION_FROZEN_EXPORT')
OUTPUT = os.getenv('ZERO_ACTION_STORE_AUDIT')


@pytest.mark.native_integration
@pytest.mark.skipif(not all((CONTAINER, REPORT, FROZEN)),
    reason='Requires explicit local Supabase container plus private frozen/replayed inputs; no production or collection fallback')
def test_real_local_store_accepts_position_facts_and_validates_candidate_pool():
    assert re.fullmatch(r'supabase_db_[A-Za-z0-9_-]+', CONTAINER)
    assert os.getenv('DOCKER_HOST', 'unix://').startswith('unix://')
    endpoint = subprocess.check_output(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], text=True).strip()
    assert endpoint.startswith('unix://'), 'Only a local Docker Unix socket is allowed'
    database = 'zero_action_store_' + uuid4().hex
    frozen = json.loads(Path(FROZEN).read_bytes())
    report = json.loads(Path(REPORT).read_bytes())
    original_export_hash = hashlib.sha256(Path(FROZEN).read_bytes()).hexdigest()
    manifest = frozen['manifest']
    assert report['id'] == manifest['job_id'] == '67cb1b78-ccb2-4fe0-80dc-c982697c10bd'
    assert report['input_manifest'] == manifest
    assert sum(f['rule_id'] == 'v22_public.market_site_domain_observed' for f in report['findings']) == 3

    def run(sql, name=database):
        __tracebackhide__ = True
        completed = subprocess.run(['docker', 'exec', '-i', CONTAINER, 'psql', '-X', '-A', '-t',
            '-U', 'postgres', '-d', name, '-v', 'ON_ERROR_STOP=1'], input=sql,
            capture_output=True, text=True, timeout=45)
        if completed.returncode:
            code = re.search(r'ERROR:\s+([A-Z][A-Z0-9_]+)', completed.stderr)
            pytest.fail('LOCAL_SQL_FAILED: ' + (code.group(1) if code else 'details withheld to protect source payloads'))
        return completed.stdout

    quote = lambda text: "'" + str(text).replace("'", "''") + "'"
    js = lambda value: quote(canonical_json_bytes(value).decode()) + '::jsonb'
    run(f'create database {database};', 'postgres')
    try:
        migrations = sorted((ROOT.parent/'search-trust/supabase/migrations').glob('*.sql'))
        # Do not change shared cluster role configuration for an isolated test.
        migrations = [p for p in migrations if p.name != '20261110130000_set_service_role_statement_timeout.sql']
        assert migrations and all('alter role ' not in p.read_text().lower() for p in migrations)
        run('\n'.join(p.read_text() for p in migrations))
        request = frozen['recipe']['request']
        identity = request['business_identity']
        context = request['evidence_input']['context']
        owner, case, job = map(quote, (manifest['user_id'], manifest['case_id'], manifest['job_id']))
        run(f"""begin;
insert into users(id,clerk_user_id,email) values({owner},'offline-store-test','offline@example.invalid');
insert into client_cases(id,user_id,site_url,normalized_domain,business_name,business_identity,operating_model,primary_service,target_market)
values({case},{owner},{quote(context['site_url'])},{quote(identity['normalized_domain'])},{quote(identity['business_name'])},{js(identity)},{quote(identity['operating_model'])},{quote(context['primary_service'])},{js(context['target_market'])});
insert into data_snapshots select * from jsonb_populate_recordset(null::data_snapshots,{js(frozen['snapshots'])});
insert into case_business_scope_versions(id,user_id,case_id,revision,scope)
values({quote(manifest['business']['id'])},{owner},{case},{manifest['business']['revision']},{js(manifest['business'])});
insert into analysis_jobs(id,case_id,job_type,current_stage,idempotency_key)
values({job},{case},'prospect_report','queued','offline-zero-action-store');
insert into workflow_charges(id,user_id,case_id,workflow_kind,idempotency_key,analysis_job_id,discovery_job_id)
values('00000000-0000-4000-8000-000000091014',{owner},{case},'prospect','offline-zero-action-store',{job},'00000000-0000-4000-8000-000000091015');
insert into analysis_input_manifests(id,user_id,case_id,job_id,business_version_id,mode,input_checksum,payload)
values({quote(manifest['id'])},{owner},{case},{job},{quote(manifest['business']['id'])},'acquisition',{quote(manifest['input_checksum'])},{js(manifest)});
insert into analysis_manifest_snapshots(manifest_id,snapshot_id,source_kind)
select {quote(manifest['id'])},(s->>'snapshot_id')::uuid,s->>'source_kind' from jsonb_array_elements({js(manifest['snapshots'])}) s;
commit;""")
        counters = "select jsonb_build_object('balance',(select credit_balance from users limit 1),'ledger_rows',(select count(*) from credit_ledger),'snapshots',(select count(*) from data_snapshots),'cost_rows',(select count(*) from job_cost_summaries));"
        before = json.loads(run(counters))
        # Real SQL functions, as the only role authorized for report persistence.
        canonical = quote(canonical_json_bytes(report).decode())
        operation = f'select public.store_v22_capability_report({owner},{case},{job},{js(report)},{canonical},1);'
        validation = f'select public.validate_v22_candidate_pool({js(report)});'
        # The internal helper is intentionally not granted to service_role.
        # Call it as the local DB owner; store invokes it through its existing
        # SECURITY DEFINER boundary. Do not alter any function privileges.
        run(validation)
        run('begin; set local role service_role; ' + operation + 'commit;')
        stored = json.loads(run(f"select jsonb_build_object('id',id,'checksum',payload_checksum,'payload',payload) from capability_report_versions where id={job};"))
        assert stored['payload'] == report
        assert stored['checksum'] == 'sha256:' + hashlib.sha256(canonical_json_bytes(report)).hexdigest()
        run('begin; set local role service_role; ' + operation + 'commit;')
        assert int(run('select count(*) from capability_report_versions;')) == 1
        # Neither an existing stored report nor its snapshot may be overwritten.
        old = frozen['report']
        run(f"""do $test$ begin
begin
perform public.store_v22_capability_report({owner},{case},{job},{js(old)},{quote(canonical_json_bytes(old).decode())},1);
raise exception 'EXPECTED_IMMUTABLE_CONFLICT';
exception when serialization_failure then
if sqlerrm <> 'CAPABILITY_REPORT_CONFLICT' then raise; end if;
end; end $test$;""")
        after = json.loads(run(counters))
        assert before == after and before['snapshots'] == 4
        assert before['ledger_rows'] == before['cost_rows'] == 0
        assert hashlib.sha256(Path(FROZEN).read_bytes()).hexdigest() == original_export_hash
        audit = dict(local_container=CONTAINER, database=database, migrations=len(migrations),
            role='service_role', helper_role='postgres (existing internal-only helper)',
            store_passed=True, candidate_pool_passed=True,
            stored_rows=1, idempotent=True, immutable_conflict_rejected=True,
            observed_fact_count=3, payload_checksum=stored['checksum'], before=before, after=after,
            local_docker_transport='unix', production_connected=False, provider_calls=0)
        if OUTPUT:
            Path(OUTPUT).write_text(json.dumps(audit, indent=2)+'\n')
        print(json.dumps(audit))
    finally:
        run(f'drop database {database};', 'postgres')
