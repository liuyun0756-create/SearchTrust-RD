"""Synthetic candidate contracts plus real, read-only v11/v12 replay."""
from copy import deepcopy
from itertools import permutations
from pathlib import Path
import json
import pytest
from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.report_v22.capability import AnalysisInputManifest, CapabilityReport, DeliveryRevision
from app.report_v22.capability_priority import apply_priority, prioritize
from app.report_v22.candidate_delivery import omission_confirmation
from app.report_v22.public_rule_catalog import HTTP, NOINDEX
from test_p0_06_finding_status import labelled_public_fixture
from test_v22_capability_contract import revision


def candidate_report(count=5, *, dependent=False):
    report=labelled_public_fixture(ruleset='v22_capability_public_v12')[2]
    raw=report.model_dump(mode='json');base=deepcopy(raw['action_candidates'][0]);fact=deepcopy(raw['findings'][0])
    raw['actions']=[];raw['findings']=[] if count else [fact]
    for n in range(count):
        f=deepcopy(fact);f.update(id=f'fn-synthetic-{n}',rule_id=NOINDEX if n==0 else 'v22_capability.service_title_review',kind='fact',status='confirmed',confidence='medium',interpretation='supported')
        a=deepcopy(base);a.update(id=f'ac-synthetic-{n}',title='Response and indexing check' if n==0 else f'Synthetic CTA review {n}',finding_ids=[f['id']],dependencies=['ac-synthetic-0'] if dependent and n==1 else [])
        a['materials'][0].update(id=f'material-synthetic-{n}',kind='technical_check' if n==0 else 'service_brief')
        raw['actions'].append(a);raw['findings'].append(f)
    return rerank(raw)


def rerank(raw):
    raw.pop('action_candidates',None);raw.pop('prioritization',None)
    apply_priority(raw,AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw['input_manifest'])))
    return CapabilityReport.model_validate_json(canonical_json_bytes(raw))


def selected(report, ids=None, visible=None):
    raw=revision(report);raw.update(schema_version='v22_delivery_revision_v3',client_action_ids=[])
    ids=ids if ids is not None else [a.id for a in report.actions]
    pool={a.id:a.model_dump(mode='json',exclude={'finding_ids','dependencies'}) for a in report.action_candidates}
    raw['actions']=[deepcopy(pool[key]) for key in ids]
    raw['client_action_ids']=ids if visible is None else visible
    return raw


def validate(raw, report):
    return DeliveryRevision.model_validate_json(canonical_json_bytes(raw)).validate_report(report)


def test_five_complete_candidates_and_three_selected_noindex_before_cta():
    report=candidate_report()
    assert len(report.action_candidates)==5 and len(report.actions)==3
    assert report.actions[0].id=='ac-synthetic-0'
    assert all(a.materials and a.steps and a.finding_ids for a in report.action_candidates)
    trace=report.prioritization.model_dump(mode='json')
    for inputs in (list(reversed(trace['candidates'])),trace['candidates']):
        assert prioritize([e['input'] for e in inputs],list(reversed([f.model_dump(mode='json') for f in report.findings])),report.input_manifest.business.model_dump(mode='json'),report.input_manifest.input_checksum,rules_version='v22_business_priority_v3')==trace


@pytest.mark.parametrize('rule',[HTTP,NOINDEX])
@pytest.mark.parametrize('patch',[{'status':'not_checked'},{'status':'not_detected'},{'confidence':'low'},{'interpretation':'needs_verification'},{'interpretation':'conflicting'},{'kind':'inference','status':'not_assessed'},{'evidence_ids':[]}])
def test_weak_technical_cannot_borrow_qualification_from_strong_other_finding(rule,patch):
    report=candidate_report();trace=report.prioritization.model_dump(mode='json');inputs=[e['input'] for e in trace['candidates']]
    findings=[f.model_dump(mode='json') for f in report.findings];findings[0].update(rule_id=rule,**patch)
    inputs[0]['finding_ids']=sorted([findings[0]['id'],findings[1]['id']])
    result=prioritize(inputs,findings,report.input_manifest.business.model_dump(mode='json'),report.input_manifest.input_checksum,rules_version='v22_business_priority_v3')
    e=next(e for e in result['candidates'] if e['input']['action_id']=='ac-synthetic-0')
    assert e['technical_finding_ids']==[] and e['prerequisite']=='none'


def test_all_weak_stay_in_pool_without_filling_client_slots():
    raw=candidate_report().model_dump(mode='json');raw['actions']=raw['action_candidates']
    for f in raw['findings']:f.update(confidence='low')
    result=rerank(raw)
    assert result.actions==[] and len(result.action_candidates)==5 and result.action_state=='insufficient_basis'


def test_replace_technical_requires_warning_confirmation_but_is_allowed():
    report=candidate_report();raw=selected(report,['ac-synthetic-4','ac-synthetic-1','ac-synthetic-2'])
    with pytest.raises(ValueError,match='confirmation'):validate(raw,report)
    raw['technical_omission_confirmation']=omission_confirmation(report,[a['id'] for a in raw['actions']],raw['client_action_ids'])
    saved=validate(raw,report)
    assert saved.technical_omission_confirmation.confirmed
    assert report.actions[0].id=='ac-synthetic-0'
    raw['actions'].reverse();raw['client_action_ids'].reverse()
    with pytest.raises(ValueError,match='confirmation'):validate(raw,report)


def test_reordering_technical_later_is_allowed_when_independent():
    report=candidate_report();raw=selected(report,['ac-synthetic-1','ac-synthetic-2','ac-synthetic-0'])
    validate(raw,report)


@pytest.mark.parametrize('case',['missing','reversed','hidden'])
def test_dependencies_cannot_be_bypassed_even_with_confirmation(case):
    report=candidate_report(dependent=True)
    ids={'missing':['ac-synthetic-4','ac-synthetic-1','ac-synthetic-2'],'reversed':['ac-synthetic-1','ac-synthetic-0','ac-synthetic-2'],'hidden':['ac-synthetic-0','ac-synthetic-1','ac-synthetic-2']}[case]
    raw=selected(report,ids,visible=ids[1:] if case=='hidden' else ids)
    ack=omission_confirmation(report,ids,raw['client_action_ids'])
    if ack:raw['technical_omission_confirmation']=ack
    with pytest.raises(ValueError,match='dependencies'):validate(raw,report)


@pytest.mark.parametrize('mutation',['append','remove','duplicate','unknown','source','material','evidence','old_revision','duplicate_visible','null_ack','false_ack'])
def test_frozen_pool_and_equal_slots_are_server_enforced(mutation):
    report=candidate_report();raw=selected(report)
    if mutation=='append':raw['actions'].append(deepcopy(raw['actions'][-1]))
    elif mutation=='remove':raw['actions'].pop();raw['client_action_ids'].pop()
    elif mutation=='duplicate':raw['actions'][1]=deepcopy(raw['actions'][0])
    elif mutation=='unknown':raw['actions'][0]['id']='another-report-candidate';raw['client_action_ids'][0]='another-report-candidate'
    elif mutation=='source':raw['source_checksum']='sha256:'+'f'*64
    elif mutation=='material':raw['actions'][0]['materials'][0]['id']='unknown'
    elif mutation=='evidence':raw['actions'][0]['materials'][0]['evidence_ids']=['invented']
    elif mutation=='old_revision':raw['schema_version']='v22_delivery_revision_v2'
    elif mutation=='duplicate_visible':raw['client_action_ids']*=2
    elif mutation=='null_ack':raw['technical_omission_confirmation']=None
    elif mutation=='false_ack':raw['technical_omission_confirmation']={'confirmed':False,'omitted_action_ids':['ac-synthetic-0'],'selected_action_ids':raw['client_action_ids'],'client_action_ids':raw['client_action_ids']}
    with pytest.raises(ValueError):validate(raw,report)


@pytest.mark.parametrize('count',[0,1,2,3])
def test_same_number_of_slots_and_restore_original(count):
    report=candidate_report(count);raw=selected(report);validate(raw,report)
    assert len(raw['actions'])==count


@pytest.mark.parametrize('case',['missing_pool','null_pool','extra_pool','forged_body','unknown_finding','unknown_evidence','trace','order','cycle'])
def test_pool_integrity(case):
    raw=candidate_report().model_dump(mode='json')
    if case=='missing_pool':raw.pop('action_candidates')
    elif case=='null_pool':raw['action_candidates']=None
    elif case=='extra_pool':raw['action_candidates'].append(deepcopy(raw['action_candidates'][-1]))
    elif case=='forged_body':raw['actions'][0]['title']='Forged selected copy'
    elif case=='unknown_finding':raw['action_candidates'][-1]['finding_ids']=['unknown']
    elif case=='unknown_evidence':raw['action_candidates'][-1]['materials'][0]['evidence_ids']=['unknown']
    elif case=='trace':raw['prioritization']['candidates'][0]['technical_finding_ids']=[]
    elif case=='order':raw['action_candidates'].reverse()
    elif case=='cycle':raw['action_candidates'][0]['dependencies']=[raw['action_candidates'][-1]['id']]
    with pytest.raises(ValueError):CapabilityReport.model_validate_json(canonical_json_bytes(raw))


def test_v11_v12_six_site_observations_and_client_order_are_identical(monkeypatch):
    from scripts.eval_gold import generate,read_json,ROOT
    from test_p0_08_gold import deny_network
    deny_network(monkeypatch)
    for p in sorted((ROOT/'tests/fixtures/gold').glob('*/input.json')):
        old,*_=generate(read_json(p),p.parent,ROOT.parent/'.release-private/2026-10-29',ruleset='v22_capability_public_v11')
        new,*_=generate(read_json(p),p.parent,ROOT.parent/'.release-private/2026-10-29',ruleset='v22_capability_public_v12')
        assert old.findings==new.findings and old.evidence==new.evidence and old.contact_checks==new.contact_checks
        eligible=lambda a:any(f.status=='confirmed' and f.confidence!='low' for f in old.findings if f.id in a.finding_ids)
        assert [a.id for a in new.actions]==[a.id for a in old.actions if eligible(a)]
        assert {a.id for a in new.action_candidates}=={e.input.action_id for e in old.prioritization.candidates}


@pytest.mark.parametrize('records,technical',[
    ([{'status_code':503}],True),
    ([{'status_code':200,'meta_robots':['noindex']}],True),
    ([{'status_code':200,'title':'Plumbing'},{'status_code':None,'check_status':'failed','error_code':'page_timeout'}],False),
])
def test_actual_saved_page_rule_gates_technical_qualification(records,technical):
    from test_v22_capability_public import inputs
    from app.report_v22.capability_public import build_public_capability_report
    manifest,request=inputs(records,ruleset='v22_capability_public_v12')
    report=build_public_capability_report(manifest,request)
    assert any(e.technical_finding_ids for e in report.prioritization.candidates)==technical
    if technical:
        assert report.prioritization.candidates[0].prerequisite=='access_indexing'
    else:
        assert not any(f.rule_id in {HTTP,NOINDEX} for f in report.findings)


def test_new_report_can_continue_service_without_enabling_new_service_ranking():
    from test_v22_capability_service import inputs
    from app.report_v22.capability_service import build_service_capability_report
    manifest,request,previous=inputs(mode='after_acquisition',public_ruleset='v22_capability_public_v12')
    assert previous['schema_version']=='2.2.7'
    report=build_service_capability_report(manifest,request,previous_report=previous)
    assert report.prioritization.rules_version=='v22_business_priority_v2'
    assert 'action_candidates' not in report.model_dump(mode='json')


def test_weak_prerequisite_is_not_promoted_to_fill_a_strong_dependent_slot():
    raw=candidate_report(dependent=True).model_dump(mode='json');raw['actions']=raw['action_candidates']
    raw['findings'][0]['confidence']='low'
    report=rerank(raw)
    assert 'ac-synthetic-0' not in [a.id for a in report.actions]
    assert 'ac-synthetic-1' not in [a.id for a in report.actions]
    entry=next(e for e in report.prioritization.candidates if e.input.action_id=='ac-synthetic-1')
    assert 'dependency_not_selected' in entry.reason_codes


def test_weak_candidate_replacement_is_hidden_by_default_but_explicit_selection_is_supported():
    raw=candidate_report().model_dump(mode='json');raw['actions']=raw['action_candidates'];raw['findings'][-1]['confidence']='low'
    report=rerank(raw);ids=['ac-synthetic-0','ac-synthetic-1','ac-synthetic-4']
    hidden=selected(report,ids,ids[:2]);validate(hidden,report)
    shown=selected(report,ids,ids);validate(shown,report)
    assert next(e for e in report.prioritization.candidates if e.input.action_id=='ac-synthetic-4').default_visible is False


@pytest.mark.parametrize('value',[1,False,'true'])
def test_technical_confirmation_requires_json_true_not_coercion(value):
    from app.report_v22.capability import TechnicalOmissionConfirmation
    with pytest.raises(ValueError):
        TechnicalOmissionConfirmation.model_validate(dict(confirmed=value,omitted_action_ids=['a'],selected_action_ids=[],client_action_ids=[]))
