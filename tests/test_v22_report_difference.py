"""Saved generated-version differences, separate from delivery wording edits."""
from copy import deepcopy
import json
import pytest
from app.jobs_v22.digest import verified_request_digest
from app.report_v22.review_report import build_verified_review_report
from app.report_v22.report_difference import build_report_difference, ReportDifference, evidence_sections, work_fields
from tests.test_v22_review_generation import generation
from tests.test_v22_review_decisions import item

def test_first_review_compares_work_but_does_not_invent_prior_observations():
    v=generation();r=build_verified_review_report(v);d=build_report_difference(v,r)
    assert d.evidence_basis=='first_review'
    assert all(a.state=='first_baseline' and not a.changed_sections for a in d.evidence)
    assert all(a.state=='unchanged' for a in d.work)
    assert d.basis.delivery_id==v.evidence.bundle.delivery_id
    assert d.basis.report_checksum==v.evidence.bundle.report_checksum

def test_only_explicit_work_changes_get_work_markers_and_no_private_reason():
    v=generation();q=item(v.evidence,'adjust');q['reason']='PRIVATE_DIFF_REASON'
    v=generation(decisions=[q]);d=build_report_difference(v,build_verified_review_report(v))
    assert d.work[0].state=='updated' and 'steps' in d.work[0].changed_fields
    assert 'PRIVATE_DIFF_REASON' not in d.model_dump_json()

def test_zero_next_actions_are_not_carried_forward_not_completed():
    v=generation(decisions=[]);d=build_report_difference(v,build_verified_review_report(v))
    assert d.work and all(a.state=='not_carried_forward' for a in d.work)
    assert all(a.after is None for a in d.work)

def test_timestamp_only_observation_and_material_references_are_not_changes():
    f=build_verified_review_report(generation()).content.seo_actions[0].facts.model_dump(mode='json')
    changed=deepcopy(f)
    for key,names in [('page',['observed_at','previous_observed_at']),('gsc',['evaluated_at']),('ga4',['started_at','completed_at'])]:
        if changed[key]:
            for name in names:changed[key][name]='2027-02-01T00:00:00Z'
    assert evidence_sections(changed)==evidence_sections(f)
    a=build_verified_review_report(generation()).actions[0].model_dump(mode='json');b=deepcopy(a)
    for m in b['materials']:m['id']='reissued';m['evidence_ids']=['reissued']
    assert work_fields(a)==work_fields(b)

def test_resigned_semantically_false_markers_are_rejected():
    v=generation();d=build_report_difference(v,build_verified_review_report(v)).model_dump(mode='json')
    d['work'][0]['state']='updated';d['work'][0]['changed_fields']=['steps']
    d['checksum']=verified_request_digest({k:x for k,x in d.items() if k!='checksum'})
    with pytest.raises(ValueError):ReportDifference.model_validate_json(json.dumps(d))

def later_generation(previous,kind='continue'):
    from unittest.mock import patch
    from uuid import UUID
    from tests.test_v22_multi_round_review import next_round,seal
    from tests.test_v22_review_decisions import request,receipt
    from app.report_v22.review_generation_models import ReviewGenerationInput
    ids=iter(UUID(f'81000000-0000-4000-8000-{i:012d}') for i in range(1,40))
    with patch('tests.test_v22_multi_round_review.uuid4',side_effect=lambda:next(ids)),patch('tests.test_v22_review_decisions.uuid4',side_effect=lambda:next(ids)):
        report,saved=next_round(previous,kind)
    decisions=[a.selected_decision.model_dump(mode='json') for a in report.content.seo_actions if a.selected_decision]+[a.model_dump(mode='json') for a in report.content.seo_new_decisions]
    req=request(saved,decisions,id=str(report.input_reference.decision_id))
    decision=receipt(saved,req,recorded_at=saved.frozen_at)
    raw=dict(schema_version='v22_review_generation_input_v1',rules_version='v22_review_generation_rules_v1',job_id=str(report.id),user_id=str(saved.user_id),case_id=str(saved.case_id),report_id=str(previous.id),prepared_at=saved.frozen_at,evidence=saved.model_dump(mode='json'),decision=decision)
    value=ReviewGenerationInput.model_validate_json(json.dumps(seal(raw)))
    assert value.checksum==report.input_reference.input_checksum
    return value,report

@pytest.mark.parametrize('kind',['continue','adjust','observe','defer','new'])
def test_later_report_has_bound_previous_facts_and_independent_work_changes(kind):
    previous=build_verified_review_report(generation());v,r=later_generation(previous,kind)
    d=build_report_difference(v,r,previous)
    assert d.evidence_basis=='previous_review'
    assert d.basis.report_id==previous.id and d.basis.report_version==previous.version
    assert d.evidence[0].before==previous.content.seo_actions[0].facts
    if kind=='continue':assert all(a.state=='unchanged' for a in d.work)
    if kind in ('observe','defer'):assert all(a.state=='not_carried_forward' for a in d.work)
    if kind=='new':assert {a.state for a in d.work}=={'added','not_carried_forward'}

@pytest.mark.parametrize('kind',['none','wrong','checksum'])
def test_missing_or_wrong_previous_report_cannot_claim_a_comparison(kind):
    previous=build_verified_review_report(generation());v,r=later_generation(previous)
    prior=None if kind=='none' else previous.model_copy(deep=True)
    if kind=='wrong':prior.id=v.job_id
    if kind=='checksum':prior.summary='different report'
    with pytest.raises(Exception):build_report_difference(v,r,prior)

def test_changed_metric_values_do_not_change_work_and_timestamp_only_checks_do_not_mark():
    from tests.support.review_projection_fixtures import bundle_raw
    from tests.test_v22_review_decisions import evidence_for
    v=generation(saved=observed_evidence());r=build_verified_review_report(v)
    f=r.content.seo_actions[0].facts.model_dump(mode='json');later=deepcopy(f)
    later['gsc']['metrics'][0]['after']+=1
    assert [k for k in evidence_sections(f) if evidence_sections(f)[k]!=evidence_sections(later)[k]]==['gsc']
    assert work_fields(r.actions[0])==work_fields(v.evidence.bundle.delivery.actions[0])
    repeat=deepcopy(f)
    repeat['page']['observed_at']='2027-01-01T00:00:00Z';repeat['page']['previous_observed_at']=repeat['page']['observed_at']
    repeat['page']['comparison']='compared';repeat['page']['comparison_reason']=None;repeat['page']['changes']=[]
    repeat['gsc']['evaluated_at']='2027-01-01T00:00:00Z'
    repeat['ga4']['started_at']='2027-01-01T00:00:00Z';repeat['ga4']['completed_at']='2027-01-01T00:01:00Z'
    assert evidence_sections(f)==evidence_sections(repeat)


def test_not_selected_sources_are_explicit_not_an_unsupported_improvement():
    from tests.support.review_projection_fixtures import bundle_raw
    from tests.test_v22_review_decisions import evidence_for
    first=build_verified_review_report(generation(saved=observed_evidence()))
    v,r=later_generation(first);d=build_report_difference(v,r,first)
    assert d.evidence[0].before.ga4.before==100 and d.evidence[0].after.ga4 is None
    assert 'ga4' in d.evidence[0].changed_sections
    assert all(a.state=='unchanged' for a in d.work)

def observed_evidence():
    from tests.support.review_projection_fixtures import bundle_raw
    from tests.test_v22_review_decisions import example
    from app.report_v22.review_freeze_models import SavedReviewEvidence
    b=bundle_raw();raw=example()[0].model_dump(mode='json')
    raw.update(user_id=b['user_id'],case_id=b['case_id'],bundle=b)
    raw['selection']=dict(report=dict(id=b['report']['id'],checksum=b['report_checksum']),delivery=dict(id=b['delivery_id'],revision=b['delivery_revision'],checksum=b['delivery_checksum']),implementation={k:b['implementation'][k] for k in ('id','revision','checksum')},actions=[dict(action_id=s['action_id'],page_current_id=s['page']['current']['id'] if s['page'] else None,page_previous_id=s['page']['previous']['id'] if s['page'] and s['page']['previous'] else None,gsc_id=s['gsc']['id'] if s['gsc'] else None,ga4_id=s['ga4']['id'] if s['ga4'] else None) for s in b['selections']])
    return SavedReviewEvidence.model_validate_json(json.dumps(raw))
