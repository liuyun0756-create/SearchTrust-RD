"""Phase 1: no network; old identities and saved reports are immutable."""
from copy import deepcopy
from pathlib import Path
import json
import pytest
from support.private_gold import require_private_gold_inputs
from test_p0_08_gold import SITE_IDS
from pydantic import ValidationError
from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_request_digest
from app.report_v22.business_identity_v2 import BusinessIdentityV2, ModeConfirmation, ModeEvidence, legacy_service_identity
from app.report_v22.operating_model import assess_mode, extract_mode_evidence
from app.report_v22.site_business_html import ParseBudget
from app.report_v22.site_business_models import SiteBusinessLimits
from app.report_v22.identity_input import prepare_identity_input, source_set_checksum
from app.report_v22.capability_public import build_public_capability_report, CapabilityPublicError
from app.report_v22.findings import build_public_findings
from app.report_v22.models import TargetMarket
from test_p0_06_finding_status import labelled_public_fixture
ROOT=Path(__file__).resolve().parents[1]


def evidence(text):
 return extract_mode_evidence(text,url='https://example.com/contact',domain='example.com',checksum='sha256:'+'a'*64,
  path='/html',budget=ParseBudget(SiteBusinessLimits()))[0]

@pytest.mark.parametrize('html,suggestion,effective',[
 ('<p>Office address: 12 Main Street.</p>',None,None),
 ('<p>We come to your home for repairs.</p>','service_area',None),
 ('<p>Visit our showroom.</p>','storefront',None),
 ('<p>Visit our showroom.</p><p>We come to your home for repairs.</p>','hybrid','hybrid'),
 ('<p>Meeting locations vary by experience and are confirmed after booking.</p>','meeting_point','meeting_point'),
 ('<p>Our office is open by appointment.</p>',None,None),
 ('<p>Customers cannot visit our showroom.</p>',None,None),
 ('<div class="testimonial"><p>Visit our showroom.</p></div>',None,None),
 ('<p hidden>Visit our showroom.</p>',None,None),
 ('<p style="display:none">Visit our showroom.</p>',None,None),
 ('<p>Visit our showroom.',None,None),
])
def test_conservative_positive_mode_evidence(html,suggestion,effective):
 row=assess_mode(evidence(html)); assert row.suggested_value==suggestion and row.value==effective

@pytest.mark.parametrize('field',[None,{'state':'not_returned','value':None},{'state':'observed','value':False},{'state':'observed','value':'false'},{'state':'observed','value':'true'}])
def test_missing_and_non_boolean_gbp_flags_are_not_facts(field):
 assert assess_mode([],gbp_field=field).value is None
 assert assess_mode([],gbp_field=field).suggested_value is None

def test_observed_positive_gbp_needs_provenance_and_does_not_infer_hybrid_from_address():
 e=ModeEvidence(kind='gbp_service_area',quote='service_area_business=true',url='https://example.com/',selector='/flag',payload_checksum='sha256:'+'a'*64)
 r=assess_mode([e],gbp_field={'state':'observed','value':True})
 assert r.suggested_value=='service_area' and r.value is None
 r=assess_mode([e,*evidence('<p>Visit our showroom.</p>')],gbp_field={'state':'observed','value':True})
 assert r.value is None and r.conflicts

def test_unavailable_and_different_entity_pages_produce_no_evidence():
 assert evidence('x'*1_000_001)==[]
 rows,notes=extract_mode_evidence('<p>Visit our showroom.</p>',url='https://other.com/',domain='example.com',checksum='sha256:'+'a'*64,path='/html',budget=ParseBudget(SiteBusinessLimits()))
 assert rows==[] and notes==['page_entity_unresolved']


def bound():
 from unittest.mock import patch
 from test_v22_capability_public import inputs
 from test_v22_site_gbp_alignment import exact_html, exact_gbp
 from site_business_helpers import source
 with patch('test_v22_capability_public.sample_input',return_value=exact_gbp()):
  return inputs(business_source=source(exact_html()),ruleset='v22_capability_public_v12')

def confirmation(q,mode):
 ref=q.evidence_input.context.customer_public_gbp
 return dict(value=mode,confirmed_by='SEO',confirmed_at='2026-11-05T00:00:00Z',reason='Confirmed with client',
  normalized_domain=q.business_identity.normalized_domain,selected_branch_ref=next((k.value for k in ref.entity_keys if k.kind=='cid'),None),
  evidence_checksum=source_set_checksum(q),source_ref='test confirmation')

@pytest.mark.parametrize('mode',['storefront','service_area','hybrid','meeting_point',None])
def test_v2_first_service_boundary_is_three_values_not_all_v2(mode):
 m,q=bound();m,q,_,_=prepare_identity_input(m,q,confirmation=confirmation(q,mode) if mode else None)
 saved=canonical_json_bytes(q.business_identity)
 market=q.evidence_input.context.target_market
 if mode in {'storefront','service_area','hybrid'}:
  result=legacy_service_identity(q.business_identity,market)
  assert result.operating_model==mode and result.primary_location==market
 else:
  with pytest.raises(ValueError,match='FIRST_SERVICE_OPERATING_MODEL_UNSUPPORTED'):legacy_service_identity(q.business_identity,market)
 assert canonical_json_bytes(q.business_identity)==saved


def test_manifest_binding_and_confirmation_cannot_cross_entities_or_sources():
 m,q=bound(); before=canonical_json_bytes(q)
 for patch in [{'normalized_domain':'other.com'},{'selected_branch_ref':'wrong'},{'evidence_checksum':'sha256:'+'0'*64}]:
  c={**confirmation(q,'hybrid'),**patch}
  with pytest.raises(ValueError):prepare_identity_input(m,q,confirmation=c)
 m2,q2,_,_=prepare_identity_input(m,q,confirmation=confirmation(q,'service_area'))
 assert canonical_json_bytes(q)==before
 assert m2.target_market==q.evidence_input.context.target_market
 assert m2.identity_checksum==request_digest(q2.business_identity)
 assert build_public_capability_report(m2,q2).schema_version=='2.2.8'
 with pytest.raises(CapabilityPublicError):build_public_capability_report(m,q2)
 with pytest.raises(CapabilityPublicError):build_public_capability_report(m2,q)
 forged=q2.model_copy(deep=True);forged.business_identity.operating_model.suggested_value='hybrid'
 with pytest.raises(CapabilityPublicError):build_public_capability_report(m2,forged)

@pytest.mark.parametrize('mode',['service_area','meeting_point',None])
def test_address_applicability_never_becomes_a_correction(mode):
 m,q=bound();m,q,_,_=prepare_identity_input(m,q,confirmation=confirmation(q,mode) if mode else None)
 result=build_public_findings(q,text_patterns=True,reviewed_evidence=True,resource_limits=True,sampling_quality=True,opening_hours=True,identity_v2=True)
 address=next(f for f in result._contact_alignment.fields if f.field=='address')
 assert address.state==('not_checked' if mode is None else 'not_applicable')
 if mode is None:assert address.not_checked_reason=='operating_model_unresolved'
 report=build_public_capability_report(m,q)
 assert not any(f.rule_id=='v22_public.gbp_address_alignment' for f in report.findings)


@pytest.mark.parametrize('name', SITE_IDS)
def test_six_real_confirmed_modes_and_address_actions(monkeypatch, name):
 from test_p0_08_gold import deny_network
 from scripts.replay_p1_05_identity import replay
 deny_network(monkeypatch)
 confirms=json.loads((ROOT/'tests/fixtures/p1-05-identity/confirmations.json').read_text())
 c=confirms[name]
 require_private_gold_inputs(ROOT/'tests/fixtures/gold'/name, ROOT.parent/'.release-private/2026-10-29')
 report,row=replay(ROOT/'tests/fixtures/gold'/name,c,ROOT.parent/'.release-private/2026-10-29')
 truth=json.loads((ROOT/'tests/fixtures/gold'/name/'truth.json').read_text())
 assert row['effective_mode']==truth['operating_model']['value']==c['value']
 if c['value'] in {'service_area','meeting_point'}:
  assert row['address']['state']=='not_applicable'
  assert row['address_actions']==[]
  assert not any(f.rule_id=='v22_public.gbp_address_alignment' for f in report.findings)
  assert all('address' not in step.lower() for a in report.action_candidates for step in a.steps if 'update the public' in step.lower())
 if name=='vision-tech-swansea':
  assert row['target_market']['city']=='Swansea'
  assert 'Llanelli' in row['operating_location']['location']['address']


def test_switching_effective_mode_removes_address_candidate_and_its_materials():
 from unittest.mock import patch
 from test_v22_capability_public import inputs
 from test_v22_site_gbp_alignment import exact_html, exact_gbp
 from site_business_helpers import source
 from app.report_v22.capability import AnalysisInputManifest
 with patch('test_v22_capability_public.sample_input',return_value=exact_gbp()):
  base,request=inputs(business_source=source(exact_html(address='999 Different Road, Austin, TX')),ruleset='v22_capability_public_v12')
 frozen=canonical_json_bytes(request)
 m,q,_,_=prepare_identity_input(base,request,confirmation=confirmation(request,'storefront'))
 # Independent synthetic SEO confirmation of this address's customer-facing use.
 q.business_identity.operating_location.role='customer_facing'
 raw=m.model_dump(mode='json');raw['identity_checksum']=request_digest(q.business_identity)
 raw['generation_input_checksum']=verified_request_digest(q.model_dump(mode='json'))
 raw['input_checksum']=verified_request_digest({k:v for k,v in raw.items() if k!='input_checksum'})
 before=build_public_capability_report(AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw)),q)
 ids={f.id for f in before.findings if f.rule_id=='v22_public.gbp_address_alignment'}
 candidates=[a for a in before.action_candidates if ids & set(a.finding_ids)]
 assert ids and candidates and all(a.materials for a in candidates)
 for mode in ('service_area','meeting_point'):
  m,q,_,_=prepare_identity_input(base,request,confirmation=confirmation(request,mode))
  after=build_public_capability_report(m,q)
  assert not any(f.rule_id=='v22_public.gbp_address_alignment' for f in after.findings)
  assert not any(a.id in {c.id for c in candidates} for a in after.action_candidates)
  assert not {material.id for c in candidates for material in c.materials} & {material.id for a in after.action_candidates for material in a.materials}
 assert canonical_json_bytes(request)==frozen


def test_mixed_branch_evidence_requires_confirmation():
 rows=evidence('<p>Visit our showroom at the Austin branch.</p><p>We come to your home for repairs.</p>')
 result=assess_mode(rows)
 assert result.value is None and result.state=='needs_confirmation' and 'branch_context_unresolved' in result.conflicts
 rows=evidence('<p>Visit our showroom.</p><p>We come to your home for repairs.</p>')
 rows[1]=rows[1].model_copy(update={'url':'https://example.com/other-location'})
 assert assess_mode(rows).value is None


def test_volcanoes_independent_contact_is_hashed_and_never_added_to_bound_input():
 from scripts.replay_p1_05_identity import saved_contact_observation
 row=saved_contact_observation()
 assert row['included_in_bound_input'] is False
 assert row['assessment']['suggested_value']=='meeting_point'
 assert any('Meeting locations vary by experience' in e['quote'] for e in row['assessment']['evidence_refs'])


def test_selected_branch_cannot_differ_from_frozen_gbp_even_with_rehashed_request():
 from app.report_v22.capability import AnalysisInputManifest
 m,q=bound();m,q,_,_=prepare_identity_input(m,q,confirmation=confirmation(q,'service_area'))
 q.business_identity.selected_branch_ref='wrong-branch'
 q.business_identity.operating_model.confirmation_ref.selected_branch_ref='wrong-branch'
 raw=m.model_dump(mode='json');raw['identity_checksum']=request_digest(q.business_identity)
 raw['generation_input_checksum']=verified_request_digest(q.model_dump(mode='json'))
 raw['input_checksum']=verified_request_digest({k:v for k,v in raw.items() if k!='input_checksum'})
 with pytest.raises(CapabilityPublicError):build_public_capability_report(AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw)),q)
