"""Run a production lineage through the acquired-input gate and real migrated SQL.

The private evidence stays outside git. The synthetic guard in
test_a3_identity_inheritance.py runs in a clean backend checkout.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from fakeredis.aioredis import FakeRedis
from pydantic import TypeAdapter
import pytest

from app.api.v2.competitor_models import CompetitorDiscoveryResult
from app.collectors.serp_market_models import SerpMarketSnapshot
from app.collectors.site_inventory_models import INVENTORY_ADAPTER
from app.competitors_v22.models import CompetitorCollectionSnapshot, SharedMarketSnapshot
from app.competitors_v22.selection import AnalysisRequestEnvelope
from app.jobs_v22.capability_acquisition_preparation import CapabilityAcquisitionPreparer, begin_acquisition_preparation
from app.jobs_v22.capability_acquisition_worker import LeasedPreparationRpc
from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository
from app.jobs_v22.capability_public_stage import CheckpointedCapabilityPublicStage
from app.jobs_v22.capability_result_persistence import SupabaseCapabilityResultPersister
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.cost_models import ALLOWED_COUNTER_KEYS
from app.jobs_v22.customer_public_gbp_stage import CustomerPublicGbpCollection
from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_request_digest
from app.jobs_v22.executor import ProspectV22Executor
from app.report_v22.findings_models import PublicFindingsInput
from app.report_v22.public_gbp_models import CustomerPublicGbpReference, CustomerPublicGbpSnapshotValue
from test_p0_08_gold import deny_network
from test_p1_01_sql_preparation import LocalSql


def _read_one(path):
    row, = json.loads(path.read_bytes())['rows']
    return row


def _retime(value, delta):
    if isinstance(value, dict):
        return {key: _retime(item, delta) for key, item in value.items()}
    if isinstance(value, list):
        return [_retime(item, delta) for item in value]
    if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:\d{2})', value):
        return (datetime.fromisoformat(value) + delta).isoformat().replace("+00:00", "Z")
    return value


def _scope(sql, scope):
    sql('insert into public.case_business_scope_versions(id,user_id,case_id,revision,scope) values ($1,$2,$3,$4,$5::jsonb)',
        scope['id'], scope['user_id'], scope['case_id'], scope['revision'], json.dumps(scope))


def _seed_history(sql, row):
    report = row['report']
    manifest = report['input_manifest']
    owner, case, old_job = manifest['user_id'], manifest['case_id'], manifest['job_id']
    _scope(sql, manifest['business'])
    sql("""insert into public.analysis_jobs
           (id,case_id,job_type,status,current_stage,idempotency_key,progress,completed_at,capability_report_id)
           values($1,$2,'prospect_report','succeeded','completed','a3-prior-confirmation',100,$3,$1)""",
        old_job, case, manifest['evaluated_at'])
    sql("""insert into public.analysis_input_manifests
           (id,user_id,case_id,job_id,business_version_id,mode,input_checksum,payload)
           values($1,$2,$3,$4,$5,'acquisition',$6,$7::jsonb)""",
        manifest['id'], owner, case, old_job, manifest['business']['id'], manifest['input_checksum'], json.dumps(manifest))
    sql("""insert into public.capability_report_versions
           (id,manifest_id,user_id,case_id,schema_version,payload_checksum,payload)
           values($1,$2,$3,$4,$5,$6,$7::jsonb)""",
        old_job, manifest['id'], owner, case, report['schema_version'], verified_request_digest(report), json.dumps(report))
    gbp = next(item for item in row['snapshots'] if item['source_kind'] == 'public_gbp')
    reference = row['recipe']['request']['evidence_input']['context']['customer_public_gbp']
    context = dict(job_id=old_job, customer_public_gbp_reference=reference,
                   subject_reference_checksum=gbp['normalized_payload']['subject_reference_checksum'])
    sql("""insert into public.data_snapshots
           (id,case_id,source_type,schema_version,fetched_at,expires_at,sync_trigger,health_status,
            normalized_payload,payload_checksum,provider_request_context)
           values($1,$2,'gbp',$3,$4,$5,'report_generation','healthy',$6::jsonb,$7,$8::jsonb)""",
        gbp['snapshot_id'], case, gbp['schema_version'], gbp['fetched_at'], gbp['expires_at'],
        json.dumps(gbp['normalized_payload']), gbp['payload_checksum'], json.dumps(context))
    sql("""insert into public.analysis_manifest_snapshots(manifest_id,snapshot_id,source_kind)
           values($1,$2,'public_gbp')""", manifest['id'], gbp['snapshot_id'])
    sql("""insert into public.case_business_identity_versions
           (id,user_id,case_id,revision,base_report_id,identity,identity_checksum)
           values($1,$2,$3,$4,$5,$6::jsonb,$7)""",
        row['pinned_identity_version_id'], owner, case, row['pinned_revision'], old_job,
        json.dumps(row['pinned_identity']), row['pinned_identity_checksum'])


@pytest.mark.anyio
@pytest.mark.parametrize('anyio_backend', ['asyncio'])
async def test_fresno_lineage_locks_identity_freezes_resolves_and_stores_v21(monkeypatch):
    lineage_dir = os.environ.get('SEARCHTRUST_A3_LINEAGE_DIR')
    if not lineage_dir:
        pytest.skip('Private Fresno lineage is required; set SEARCHTRUST_A3_LINEAGE_DIR for the migrated SQL replay.')
    workspace = Path(os.environ['SEARCHTRUST_SQL_WORKSPACE']).resolve()
    assert shutil.which('node') and (workspace / 'node_modules/@electric-sql/pglite').exists()
    root = Path(lineage_dir).resolve()
    prior = _read_one(root / 'fresno-confirmed-production.json')
    current = _read_one(root.parent / '2026-11-20-identity-diagnosis/fresno-production.json')
    original = _read_one(root / 'a3-fresno-v20-preparation.json')
    deny_network(monkeypatch)

    base = current['report']['input_manifest']
    refs = {source['kind']: source for source in current['recipe']['request']['evidence_input']['sources']}
    snapshots = {item['source_kind']: item['normalized_payload'] for item in current['snapshots']}
    for kind, reference in refs.items():
        reference['payload'] = snapshots[kind]
        if reference.get('shared_snapshot'):
            reference['shared_snapshot']['snapshot'] = snapshots[kind]
    frozen = PublicFindingsInput.model_validate_json(canonical_json_bytes(current['recipe']['request']))
    assert verified_request_digest(frozen.model_dump(mode='json')) == base['generation_input_checksum']
    for source in frozen.evidence_input.sources:
        assert request_digest(source.payload) == source.binding.payload_checksum

    replay_now = datetime.now(timezone.utc)
    shift = replay_now - timedelta(seconds=5) - datetime.fromisoformat(base['evaluated_at'])
    created = datetime.fromisoformat(original['created_at']) + shift
    deadline = datetime.fromisoformat(original['deadline_at']) + shift
    scope = _retime(base['business'], shift)
    raw = _retime(original['request'], shift)
    owner, case, job = (UUID(base[key]) for key in ('user_id', 'case_id', 'job_id'))
    market_body = SerpMarketSnapshot.model_validate_json(canonical_json_bytes(_retime(snapshots['serp'], shift)))
    shared = SharedMarketSnapshot.model_validate_json(canonical_json_bytes({
        **_retime({key: value for key, value in refs['serp']['shared_snapshot'].items() if key != 'snapshot'}, shift),
        'snapshot': market_body.model_dump(mode='json'), 'snapshot_checksum': request_digest(market_body)}))
    raw['competitor_discovery']['market_snapshot_checksum'] = shared.snapshot_checksum
    envelope = AnalysisRequestEnvelope.model_validate_json(canonical_json_bytes(raw))
    request = envelope.analyze_request
    discovery = CompetitorDiscoveryResult(**envelope.competitor_discovery.model_dump(),
        case_id=case, input_digest=shared.input_digest, candidates=[], ready_for_confirmation=False,
        data_gaps=[dict(gap_code='OFFLINE_REPLAY', message='Provider responses are frozen', blocking=True, resolution='Use saved evidence')],
        created_at=shared.created_at, expires_at=shared.expires_at)
    rivals = CompetitorCollectionSnapshot.model_validate_json(canonical_json_bytes({**_retime(snapshots['competitor'], shift),
        'market_snapshot_checksum': shared.snapshot_checksum}))
    gbp_reference = CustomerPublicGbpReference.model_validate_json(canonical_json_bytes(_retime(
        current['recipe']['request']['evidence_input']['context']['customer_public_gbp'], shift)))
    gbp = TypeAdapter(CustomerPublicGbpSnapshotValue).validate_json(canonical_json_bytes({**_retime(snapshots['public_gbp'], shift),
        'subject_reference_checksum': request_digest(gbp_reference)}))

    stages = dict(
        discovery_store=SimpleNamespace(require_state=AsyncMock(return_value=SimpleNamespace(status='succeeded', result=discovery))),
        market_store=SimpleNamespace(get=AsyncMock(return_value=shared)),
        site_stage=SimpleNamespace(collect=AsyncMock(return_value=INVENTORY_ADAPTER.validate_json(canonical_json_bytes(_retime(snapshots['site'], shift))))),
        customer_public_gbp_stage=SimpleNamespace(collect=AsyncMock(return_value=CustomerPublicGbpCollection(
            schema_version='customer_public_gbp_checkpoint_' + gbp.schema_version.rsplit('_', 1)[-1],
            reference=gbp_reference, snapshot=gbp))),
        competitor_stage=SimpleNamespace(collect=AsyncMock(return_value=rivals)),
    )
    bridge = LocalSql(workspace, Path(__file__).parent / 'support/a3_sql_replay.mjs')

    def sql(statement, *parameters):
        return bridge.command(dict(op='query', sql=statement, parameters=parameters))

    redis = FakeRedis(decode_responses=False)
    try:
        bridge.command(dict(op='init', seed=dict(owner=str(owner), caseId=str(case),
            identity=request.business_identity.model_dump(mode='json'), service=request.primary_service,
            market=request.target_market.model_dump(mode='json'))))
        sql("begin")
        _seed_history(sql, prior)
        sql("commit")
        _scope(sql, scope)
        sql("""insert into public.analysis_jobs (id,case_id,job_type,status,current_stage,idempotency_key,created_at,deadline_at)
               values($1,$2,'prospect_report','queued','queued','a3-fresno-replay',$3,$4)""", str(job), str(case), created.isoformat(), deadline.isoformat())
        keyword_scope = dict(primary_service=request.primary_service, target_market=request.target_market.model_dump(mode='json'))
        planning = dict(schema_version='v22_planning_settings_v2', priority=None, target_pages=[],
            execution_conditions=dict(capacity='unknown', publishing_access='unknown'), keyword_scope=keyword_scope,
            keyword_confirmation=dict(confirmed_by=str(owner), confirmed_at=scope['confirmed_at']),
            target_keywords=[dict(query=query, origin='seo', suggestion_refs=[]) for query in request.queries])
        revision = envelope.target_keywords.planning_revision
        sql('insert into public.case_planning_settings(case_id,user_id,revision,settings) values($1,$2,$3,$4::jsonb)',
            str(case), str(owner), revision, json.dumps(planning))
        await bridge.post('start_v22_prospect_discovery_with_keywords', dict(
            p_user_id=str(owner), p_case_id=str(case), p_workflow_id=str(job), p_discovery_job_id=str(discovery.discovery_id),
            p_workflow_idempotency_key='a3-fresno-replay', p_task_idempotency_key='a3-discovery-replay',
            p_planning_revision=revision, p_queries_canonical=canonical_json_bytes(request.queries).decode(),
            p_keyword_scope=keyword_scope))
        sql('update public.workflow_charges set analysis_job_id=$1 where id=$1', str(job))


        repository = SupabaseCapabilityInputRepository(url='https://unused.example', service_role_key='local-test', http_client=None)
        repository.rpc = bridge
        args = dict(user_id=owner, job_id=job, idempotency_key='a3-fresno-replay',
                    business_version_id=UUID(scope['id']), request=envelope)
        identity, _ = await begin_acquisition_preparation(repository, **args)
        assert sql('select pinned_identity_version_id from public.capability_acquisition_preparations where job_id=$1', str(job)) == [
            dict(pinned_identity_version_id=prior['pinned_identity_version_id'])]
        token = uuid4()
        claim = await bridge.post('claim_v22_acquisition_preparation',
            dict(p_job_id=str(job), p_run_generation=1, p_lease_token=str(token), p_max_attempts=3))
        assert claim['outcome'] == 'claimed'

        async def cost_snapshot():
            return {**dict.fromkeys(ALLOWED_COUNTER_KEYS, 0), 'cost_schema_version': 1, 'cost_ledger_revision': 3}

        repository.rpc = LeasedPreparationRpc(bridge, token=token, before_freeze=cost_snapshot)
        executor = ProspectV22Executor(**stages, report_pipeline=None, clock=lambda: replay_now)
        preparer = CapabilityAcquisitionPreparer(repository=repository, collector=executor,
            discovery_verifier=SimpleNamespace(verify=AsyncMock(return_value=envelope.competitor_discovery)),
            clock=lambda: replay_now, target_keywords_enabled=True, gbp_comparison_enabled=True,
            landing_page_comparison_enabled=True, comparison_reasons_enabled=True,
            local_entity_coverage_enabled=True, action_example_copy_enabled=True)
        dispatch = await preparer.prepare(**args,
            checkpoints=SimpleNamespace(run_generation=1, report_progress=AsyncMock()),
            cost_ledger=SimpleNamespace(job_id=job, job_created_at=identity.created_at))
        proof = await repository.resolve(job_id=job, request=dispatch.request.capability_request, run_generation=1)
        manifest = proof.manifest
        inheritance = manifest.identity_inheritance
        assert manifest.ruleset_version == 'v22_capability_public_v21'
        assert inheritance.source_revision == 4
        assert inheritance.confirmed_at == prior['pinned_identity']['operating_model']['confirmation_ref']['confirmed_at']
        assert (inheritance.branch, inheritance.operating_model, inheritance.visits) == ('retained',) * 3
        assert proof.request.business_identity.operating_model.value == 'hybrid'
        assert proof.request.business_identity.operating_location.role == 'customer_facing'
        assert manifest.generation_input_checksum == verified_request_digest(proof.request.model_dump(mode='json'))
        for item in proof.request.evidence_input.sources:
            assert request_digest(item.payload) == item.binding.payload_checksum

        checkpoints = JobCheckpoints(redis, prefix='local-a3-replay', ttl_seconds=600)
        report = await CheckpointedCapabilityPublicStage().build(
            job_id=job, manifest=manifest, request=proof.request, checkpoints=checkpoints)
        assert all(getattr(report, name) is not None for name in ('keyword_visibility', 'gbp_comparison',
            'landing_page_comparison', 'possible_reasons', 'local_entity_coverage', 'action_advice'))
        assert {c.field: c.result for c in report.contact_checks if c.field in {'business_name', 'address', 'phone'}} == {
            'business_name': 'match', 'address': 'match', 'phone': 'match'}
        persister = SupabaseCapabilityResultPersister(url='https://unused.example', service_role_key='local-test', http_client=None)
        persister.rpc = bridge
        completion = await persister.persist(user_id=owner, case_id=case, job_id=job,
                                             run_generation=1, manifest=manifest, report=report)
        assert not completion.idempotent and completion.report_id == job
        assert report.schema_version == '2.2.16'
        assert sql("""select r.schema_version, r.payload_checksum, w.state from public.capability_report_versions r
                      join public.workflow_charges w on w.final_capability_report_id=r.id where r.id=$1""", str(job)) == [
            dict(schema_version='2.2.16', payload_checksum=completion.payload_checksum, state='consumed')]
        calls = [name for name, _ in bridge.calls]
        assert calls.count('freeze_v22_acquisition_preparation') == 1
        assert calls.index('get_v22_acquisition_identity_pin') < calls.index('freeze_v22_acquisition_preparation')
        assert calls.index('freeze_v22_acquisition_preparation') < calls.index('resolve_v22_capability_analysis_input')
        assert calls.index('resolve_v22_capability_analysis_input') < calls.index('complete_v22_capability_analysis')
    finally:
        await redis.aclose()
        bridge.close()
