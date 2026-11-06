"""Offline mode edits retain sources, rebuild related recommendations and require approval."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID
import pytest
from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.public_reassessment import reassess_public_report
from app.preflight_v22.identity_review import IdentityReviewCommand, IdentityReviewService
from test_p1_05_identity import bound
from test_p0_08_gold import deny_network

@pytest.fixture
def anyio_backend(): return 'asyncio'

REQUEST=UUID('11111111-1111-4111-8111-111111111111')
def base():
    m,q=bound();return build_public_capability_report(m,q),q

def selection(q,mode='meeting_point'):
    k=q.evidence_input.context.customer_public_gbp.entity_keys[0]
    return dict(operating_model=mode,branch_ref=f'{k.kind}:{k.value}',reason='Confirmed with the business owner')

@pytest.mark.parametrize('mode',['storefront','hybrid','service_area','meeting_point'])
def test_saved_sources_and_base_bytes_unchanged_with_new_report(mode,monkeypatch):
    deny_network(monkeypatch)
    r,q=base();before=canonical_json_bytes(r)+canonical_json_bytes(q)
    after,updated,diff=reassess_public_report(r,q,selection(q,mode),request_id=REQUEST,evaluated_at='2026-11-05T12:00:00Z')
    assert canonical_json_bytes(r)+canonical_json_bytes(q)==before
    assert canonical_json_bytes(q.evidence_input)==canonical_json_bytes(updated.evidence_input)
    assert after.id!=r.id and after.version==r.version+1
    assert after.input_manifest.evidence_as_of==r.input_manifest.evaluated_at
    assert after.settlement_policy=='no_charge_public_reassessment'
    assert diff['requires_approval'] and not diff['copy_migrated']
    address=next(c for c in after.contact_checks if c.field=='address')
    assert address.status==('not_assessed' if mode in {'service_area','meeting_point'} else 'not_checked')
    assert not any(f.rule_id=='v22_public.gbp_address_alignment' for f in after.findings)


def test_cannot_swap_frozen_branch_or_forge_base():
    r,q=base()
    with pytest.raises(ValueError,match='SAVED_BRANCH_DETAILS_REQUIRED'):
        reassess_public_report(r,q,{**selection(q),'branch_ref':'cid:999'},request_id=REQUEST,evaluated_at='2026-11-05T12:00:00Z')
    bad=q.model_copy(deep=True);bad.business_identity.business_name='Unrelated'
    with pytest.raises(ValueError,match='BASE_INPUT_MISMATCH'):
        reassess_public_report(r,bad,selection(q),request_id=REQUEST,evaluated_at='2026-11-05T12:00:00Z')

class Repository:
    def __init__(self,r,q):
        self.bundle=dict(report=r.model_dump(mode='json'),request=q.model_dump(mode='json'),identity_revision=0,delivery_revision=0)
        self.prepared={};self.calls=[]
    async def call(self,op,cmd,**kwargs):
        self.calls.append(op)
        if op=='workspace':return deepcopy(self.bundle)
        if op=='prepared':return self.prepared.get(kwargs['p_request_id'])
        if op=='preview':
            value={**kwargs['p_preview'],'request_id':kwargs['p_request_id'],'state':'preview'}
            self.prepared[kwargs['p_request_id']]=value;return value
        raise AssertionError(op)

@pytest.mark.anyio
async def test_production_service_workspace_preview_idempotence_owner_and_conflict(monkeypatch):
    deny_network(monkeypatch)
    r,q=base();repo=Repository(r,q)
    service=IdentityReviewService(repo,clock=lambda:datetime(2026,11,5,12,tzinfo=timezone.utc))
    common=dict(user_id=r.input_manifest.user_id,case_id=r.input_manifest.case_id,report_id=r.id)
    workspace=await service.run(IdentityReviewCommand(operation='workspace',**common))
    assert workspace['lookup_attempts']==0 and workspace['branches']
    command=IdentityReviewCommand(operation='preview',**common,request_id=REQUEST,
        selection=selection(q),base_checksum=workspace['base_checksum'])
    result=await service.run(command)
    assert result==await service.run(command)
    assert repo.calls.count('preview')==1
    with pytest.raises(ValueError,match='CONFLICT'):
        await service.run(command.model_copy(update={'request_id':UUID(int=12),'expected_identity_revision':1}))
    with pytest.raises(ValueError,match='UNAVAILABLE'):
        await service.run(IdentityReviewCommand(operation='workspace',**{**common,'user_id':UUID(int=99)}))

@pytest.mark.anyio
async def test_ledger_is_persisted_before_provider_and_restored_after_restart():
    from test_p1_05_gbp_identity import request_for, SavedProvider
    from app.preflight_v22.gbp_identity import IdentityGbpLookup
    from app.preflight_v22.gbp_identity_models import GbpLookupLedger
    saved=None;writes=[]
    async def persist(ledger,*,reserve):
        nonlocal saved
        if reserve:
            assert len(ledger.attempts)==(len(saved.attempts) if saved else 0)+1
        saved=ledger.model_copy(deep=True);writes.append((reserve,len(saved.attempts)))
    class Interrupted:
        async def request(self,params,*,before_attempt,after_attempt):
            await before_attempt(1,'fake')
            assert saved and saved.attempts[-1].outcome=='started'
            raise TimeoutError('interrupted')
    req=request_for();req.gbp_url='https://maps.google.com/?cid=123'
    await IdentityGbpLookup(provider=Interrupted(),incremental_enabled=True,persist_ledger=persist).lookup(req)
    assert len(saved.attempts)==1
    # A completely new service instance must receive the database ledger.
    provider=SavedProvider({'local_results':[]},{'local_results':[]})
    resumed=await IdentityGbpLookup(provider=provider,incremental_enabled=True,persist_ledger=persist).lookup(req,ledger=GbpLookupLedger.model_validate(saved.model_dump()))
    assert len(resumed.ledger.attempts)==3 and len(provider.calls)==2
    again=await IdentityGbpLookup(provider=provider,incremental_enabled=True,persist_ledger=persist).lookup(req,ledger=saved)
    assert len(provider.calls)==2 and len(again.ledger.attempts)==3
    assert [n for reserve,n in writes if reserve]==[1,2,3]

@pytest.mark.anyio
async def test_failed_ledger_reservation_prevents_any_provider_send():
    from test_p1_05_gbp_identity import request_for,SavedProvider
    from app.preflight_v22.gbp_identity import IdentityGbpLookup
    async def rejected(*args,**kwargs):raise ValueError('LOOKUP_CONFLICT')
    provider=SavedProvider({'local_results':[]})
    await IdentityGbpLookup(provider=provider,incremental_enabled=True,persist_ledger=rejected).lookup(request_for())
    assert provider.calls==[]

@pytest.mark.anyio
async def test_real_api_is_authenticated_bounded_and_owner_command_is_strict(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from pydantic import SecretStr
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.api.v2 import identity_review as api
    from app.core.config import settings
    from app.jobs_v22.errors import DeterministicJobError
    app=FastAPI();app.include_router(api.router)
    mock=SimpleNamespace(run=AsyncMock(return_value={'ok':True}))
    async def service():yield mock
    app.dependency_overrides[api.service]=service
    monkeypatch.setattr(settings,'V22_INTERNAL_API_TOKEN',SecretStr('local-identity-token'))
    monkeypatch.setattr(settings,'V22_CAPABILITY_EXECUTION_ENABLED',True)
    r,q=base();body=dict(operation='workspace',user_id=str(r.input_manifest.user_id),case_id=str(r.input_manifest.case_id),report_id=str(r.id))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        assert (await c.post('/api/v2/identity-review',json=body)).status_code==401
        h={'Authorization':'Bearer local-identity-token'}
        assert (await c.post('/api/v2/identity-review',json=body,headers=h)).status_code==200
        assert (await c.post('/api/v2/identity-review',json=body|{'ledger':{}},headers=h)).status_code==400
        assert (await c.post('/api/v2/identity-review',content=b'x'*9000,headers=h|{'Content-Type':'application/json'})).status_code==413
        mock.run.side_effect=DeterministicJobError('V22_IDENTITY_REVIEW_REJECTED','private database detail')
        response=await c.post('/api/v2/identity-review',json=body,headers=h)
        assert response.status_code==409 and response.json()=={'code':'CONFLICT'}

@pytest.mark.parametrize('mode',['service_area','meeting_point'])
def test_reassessment_removes_confirmed_address_action_and_materials_together(mode,monkeypatch):
    from unittest.mock import patch
    from test_v22_capability_public import inputs
    from test_v22_site_gbp_alignment import exact_html, exact_gbp
    from site_business_helpers import source
    from test_p1_05_identity import confirmation
    from app.report_v22.identity_input import prepare_identity_input
    from app.report_v22.capability import AnalysisInputManifest
    from app.jobs_v22.digest import request_digest
    deny_network(monkeypatch)
    with patch('test_v22_capability_public.sample_input',return_value=exact_gbp()):
        m,q=inputs(business_source=source(exact_html(address='999 Different Road, Austin, TX')),ruleset='v22_capability_public_v12')
    m,q,_,_=prepare_identity_input(m,q,confirmation=confirmation(q,'hybrid'))
    q.business_identity.operating_location.role='customer_facing'
    raw=m.model_dump(mode='json');raw['identity_checksum']=request_digest(q.business_identity)
    raw['generation_input_checksum']=verified_request_digest(q.model_dump(mode='json'))
    raw['input_checksum']=verified_request_digest({k:v for k,v in raw.items() if k!='input_checksum'})
    before=build_public_capability_report(AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw)),q)
    ids={f.id for f in before.findings if f.rule_id=='v22_public.gbp_address_alignment'}
    removed=[a for a in before.action_candidates if ids.intersection(a.finding_ids)]
    assert removed and any(a.materials for a in removed)
    after,updated,diff=reassess_public_report(before,q,selection(q,mode),request_id=REQUEST,evaluated_at='2026-11-05T12:00:00Z')
    assert not any(f.rule_id=='v22_public.gbp_address_alignment' for f in after.findings)
    assert not {a.id for a in removed}.intersection(a.id for a in after.action_candidates)
    assert {a.id for a in removed}.issubset(diff['removed_action_ids'])
    assert not {x.id for a in removed for x in a.materials}.intersection(x.id for a in after.action_candidates for x in a.materials)

@pytest.mark.anyio
async def test_real_lookup_entry_reloads_case_ledger_between_requests():
    from test_p1_05_gbp_identity import SavedProvider
    from app.preflight_v22.gbp_identity import IdentityGbpLookup
    r,q=base()
    class LedgerRepository(Repository):
        async def call(self,op,cmd,**kw):
            if op=='ledger':
                saved=kw['p_ledger'];prior=self.bundle.get('ledger',{'attempts':[]})
                assert len(saved['attempts'])==len(prior['attempts'])+(1 if kw['p_reserve'] else 0)
                self.bundle['ledger']=deepcopy(saved);return saved
            if op=='lookup':return kw['p_result']
            return await super().call(op,cmd,**kw)
    repo=LedgerRepository(r,q)
    # Original sample has expired at the new lookup time; only synthetic
    # responses may be attempted. Every new service reloads the Case ledger.
    provider=SavedProvider(*[{'local_results':[]} for _ in range(3)])
    command=IdentityReviewCommand(operation='lookup',user_id=r.input_manifest.user_id,case_id=r.input_manifest.case_id,report_id=r.id)
    for _ in range(3):
        service=IdentityReviewService(repo,lookup_factory=lambda:IdentityGbpLookup(provider=provider,incremental_enabled=True),clock=lambda:datetime(2026,11,20,tzinfo=timezone.utc))
        await service.run(command)
    assert len(provider.calls)==3
    assert len(repo.bundle['ledger']['attempts'])==3

@pytest.mark.parametrize('mode',['storefront','hybrid'])
@pytest.mark.parametrize('website_address,expected',[('123 Fixture St, Austin, TX','match'),('999 Different Road, Austin, TX','mismatch')])
def test_explicit_address_visit_confirmation_runs_comparison_and_records_provenance(mode,website_address,expected,monkeypatch):
    from unittest.mock import patch
    from test_v22_capability_public import inputs
    from test_v22_site_gbp_alignment import exact_html,exact_gbp
    from site_business_helpers import source
    deny_network(monkeypatch)
    with patch('test_v22_capability_public.sample_input',return_value=exact_gbp()):
        m,q=inputs(business_source=source(exact_html(address=website_address)),ruleset='v22_capability_public_v12')
    before=build_public_capability_report(m,q);saved=canonical_json_bytes(q)
    unchecked,unknown,_=reassess_public_report(before,q,selection(q,mode),request_id=REQUEST,evaluated_at='2026-11-06T00:00:00Z')
    assert unknown.business_identity.operating_location.role=='unknown'
    assert next(c for c in unchecked.contact_checks if c.field=='address').status=='not_checked'
    checked,updated,_=reassess_public_report(before,q,{**selection(q,mode),'customers_can_visit':True},request_id=REQUEST,evaluated_at='2026-11-06T00:00:00Z')
    location=updated.business_identity.operating_location
    assert location.role=='customer_facing' and location.state=='seo_confirmed'
    assert location.confirmation_ref.confirmed_by==str(m.user_id)
    assert location.confirmation_ref.confirmed_at=='2026-11-06T00:00:00Z'
    assert location.confirmation_ref.selected_branch_ref==updated.business_identity.selected_branch_ref
    assert location.confirmation_ref.address==location.location.address=='123 Fixture St, Austin, TX'
    assert next(c for c in checked.contact_checks if c.field=='address').result==expected
    assert canonical_json_bytes(q)==saved
    # A later review never inherits a tick from a prior identity version.
    again,reset,_=reassess_public_report(checked,updated,selection(q,mode),request_id=UUID(int=123),evaluated_at='2026-11-06T01:00:00Z')
    assert reset.business_identity.operating_location.role=='unknown'
    assert reset.business_identity.operating_location.confirmation_ref is None
    assert next(c for c in again.contact_checks if c.field=='address').status=='not_checked'

@pytest.mark.parametrize('mode',['service_area','meeting_point'])
def test_customer_visit_confirmation_rejected_for_non_storefront_modes(mode):
    from pydantic import ValidationError
    from app.report_v22.public_reassessment import IdentitySelection
    _,q=base()
    with pytest.raises(ValidationError):IdentitySelection.model_validate({**selection(q,mode),'customers_can_visit':True})
    assert not IdentitySelection.model_validate(selection(q,mode)).customers_can_visit

@pytest.mark.parametrize('reason',['',' ','\n\t'])
def test_confirmation_reason_is_required_at_server_boundary(reason):
    from pydantic import ValidationError
    from app.report_v22.public_reassessment import IdentitySelection
    _,q=base()
    with pytest.raises(ValidationError):IdentitySelection.model_validate({**selection(q),'reason':reason})


def test_pr_saved_parse_failure_is_not_converted_into_a_match_by_visit_confirmation(monkeypatch):
    from scripts.eval_gold import generate,read_json
    from pathlib import Path
    from app.report_v22.identity_input import prepare_identity_input
    from app.report_v22.findings import build_public_findings
    deny_network(monkeypatch)
    root=Path(__file__).resolve().parents[1];directory=root/'tests/fixtures/gold/pr-plumbing-fresno'
    before,_,q,*_=generate(read_json(directory/'input.json'),directory,root.parent/'.release-private/2026-10-29',ruleset='v22_capability_public_v12')
    m,q,_,_=prepare_identity_input(before.input_manifest,q);before=build_public_capability_report(m,q)
    after,updated,_=reassess_public_report(before,q,{**selection(q,'hybrid'),'customers_can_visit':True},request_id=REQUEST,evaluated_at='2026-11-06T00:00:00Z')
    assert updated.business_identity.operating_location.role=='customer_facing'
    check=next(c for c in after.contact_checks if c.field=='address')
    assert check.status=='not_checked' and check.result=='none'
    findings=build_public_findings(updated,text_patterns=True,reviewed_evidence=True,rendered_pages=any(s.kind=='rendered' for s in updated.evidence_input.sources),resource_limits=True,sampling_quality=True,opening_hours=True,identity_v2=True)
    address=next(c for c in findings._contact_alignment.fields if c.field=='address')
    assert address.not_checked_reason=='site_content_not_checked'
    assert 'address:html_parse_failed' in address.limitations
