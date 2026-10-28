"""Bounded plan projection and later-round semantic replay; no external I/O."""
from copy import deepcopy
from uuid import uuid4
import pytest
from app.jobs_v22.digest import canonical_json_bytes,verified_request_digest
from app.report_v22.review_report import build_verified_review_report
from app.report_v22.review_plan_basis import review_plan_basis,parse_plan_report,PlanBasisReport
from app.report_v22.review_freeze_models import assemble_bundle,SavedReviewEvidence,ReviewFreezeSelection
from app.report_v22.review_generation_models import ReviewGenerationInput
from app.report_v22.review_projection import build_review_projection
from app.report_v22.implementation_records import parse_implementation_record
from tests.test_v22_review_generation import generation
from tests.test_v22_review_decisions import request,item,receipt


def seal(raw):return raw|dict(checksum=verified_request_digest(raw))

def next_round(report,kind='continue',day=2,long=False):
    basis=review_plan_basis(report);owner=dict(user_id=str(report.business.user_id),case_id=str(report.business.case_id),report_id=str(report.id))
    delivery=dict(schema_version='v22_review_delivery_revision_v1',**owner,expected_revision=0,source_checksum=verified_request_digest(report.model_dump(mode='json')),status='approved',title='Approved plan',summary='Next work agreed.',client_note=None,actions=[a.model_dump(mode='json') for a in report.actions])
    if long and delivery['actions']:delivery['actions'][0]['steps']=['Approved work. '*220]
    delivery_id=uuid4();at=f'2027-01-{day:02d}T12:00:00Z'
    record=seal(dict(schema_version='v22_implementation_record_v1',id=str(uuid4()),**owner,report_checksum=delivery['source_checksum'],delivery_revision_id=str(delivery_id),delivery_revision=1,revision=1,recorded_at=at,entries=[dict(action_id=a['id'],scope=a['target'],implemented_on=f'2027-01-{day:02d}',timezone='UTC',note=None,responsible=None) for a in delivery['actions']]))
    core=dict(user_id=owner['user_id'],case_id=owner['case_id'],evaluated_at=at,rules_version='v22_review_projection_rules_v1',report=basis.model_dump(mode='json'),report_checksum=delivery['source_checksum'],delivery_id=str(delivery_id),delivery_revision=1,delivery=delivery,delivery_checksum=verified_request_digest(delivery),implementation=record,selections=[dict(action_id=a['id'],page=None,gsc=None,ga4=None) for a in delivery['actions']])
    bundle=assemble_bundle(core)
    choice=ReviewFreezeSelection.model_validate_json(canonical_json_bytes(dict(report=dict(id=str(report.id),checksum=delivery['source_checksum']),delivery=dict(id=str(delivery_id),revision=1,checksum=verified_request_digest(delivery)),implementation=dict(id=record['id'],revision=1,checksum=record['checksum']),actions=[dict(action_id=a['id'],page_current_id=None,page_previous_id=None,gsc_id=None,ga4_id=None) for a in delivery['actions']])))
    saved=SavedReviewEvidence.model_validate_json(canonical_json_bytes(dict(schema_version='v22_saved_review_evidence_v1',id=str(uuid4()),user_id=owner['user_id'],case_id=owner['case_id'],selection=choice.model_dump(mode='json'),bundle=bundle.model_dump(mode='json'),frozen_at=at)))
    req=request(saved,[] if not delivery['actions'] else [item(saved,kind,'new-this-round' if kind=='new' else None)])
    decision=receipt(saved,req,recorded_at=at)
    value=ReviewGenerationInput.model_validate_json(canonical_json_bytes(seal(dict(schema_version='v22_review_generation_input_v1',rules_version='v22_review_generation_rules_v1',job_id=str(uuid4()),**owner,prepared_at=at,evidence=saved.model_dump(mode='json'),decision=decision))))
    return build_verified_review_report(value),saved


def test_bounded_basis_has_distinct_digest_and_no_recursive_or_private_payload():
    first=build_verified_review_report(generation());basis=review_plan_basis(first);raw=basis.model_dump(mode='json')
    assert basis.source_checksum==verified_request_digest(first.model_dump(mode='json')) and basis.checksum!=basis.source_checksum
    assert 'input_reference' not in raw and 'content' not in raw and 'selected_decision' not in raw
    assert isinstance(parse_plan_report(raw),PlanBasisReport)
    assert basis.prior_diagnosis.model_dump(mode='json')==first.content.prior_diagnosis.model_dump(mode='json')

@pytest.mark.parametrize('kind',['continue','adjust','observe','defer','new'])
def test_later_round_uses_only_approved_plan_and_explicit_new_decision(kind):
    first=build_verified_review_report(generation());r,saved=next_round(first,kind)
    assert r.version==first.version+1 and r.input_reference.report_id==first.id
    assert r.content.prior_diagnosis==first.content.prior_diagnosis
    assert r.content.seo_actions[0].facts.recorded_work.state=='seo_recorded'
    assert r.content.seo_actions[0].facts.page is None and r.content.seo_actions[0].facts.gsc is None
    assert bool(r.actions)==(kind in ['continue','adjust','new'])
    assert r.content.effect is None and not r.content.causal_attribution
    assert 'Review the contact path.' not in r.content.model_dump_json(include={'client_actions','client_new_decisions'})


def test_many_rounds_preserve_long_approved_work_without_recursive_growth():
    r=build_verified_review_report(generation());root=r.content.prior_diagnosis;sizes=[]
    for day in range(2,9):
        r,s=next_round(r,day=day,long=day==2)
        assert len(r.actions[0].steps[0])>2000 and r.content.prior_diagnosis==root
        sizes.append(len(r.model_dump_json()))
    assert max(sizes)-min(sizes)<1000

@pytest.mark.parametrize('field',['id','source_checksum','action_state','actions','business','checksum'])
def test_plan_tampering_never_reuses_original_basis_digest(field):
    raw=review_plan_basis(build_verified_review_report(generation())).model_dump(mode='json')
    if field in ('id','source_checksum','checksum'):raw[field]=str(uuid4()) if field=='id' else 'sha256:'+'0'*64
    elif field=='actions':raw[field][0]['target']='Another page'
    elif field=='business':raw[field]['case_id']=str(uuid4())
    else:raw[field]='no_next_actions'
    with pytest.raises(ValueError):parse_plan_report(raw)
