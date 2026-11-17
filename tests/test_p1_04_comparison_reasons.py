from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
import pytest
from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.comparison_reasons import build_possible_reasons, prepare_reasons_input, select_reasons_ruleset
from app.report_v22.gbp_comparison import GbpComparison, compare_rows
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.capability import CapabilityReport
from test_p1_02_gbp_comparison import comparison_input
from app.report_v22.landing_page_comparison import prepare_landing_input

def landing_input():
    from findings_helpers import seal
    from test_p1_01_keyword_visibility import reseal
    from app.report_v22.operating_model import inspect_operating_model
    from app.jobs_v22.digest import request_digest
    m,q=comparison_input()
    i=next(i for i,s in enumerate(q.evidence_input.sources) if s.kind=='public_gbp')
    source=q.evidence_input.sources[i];source.payload.record.fields.review_count.value=50
    q.evidence_input.sources[i]=seal(source)
    q.business_identity.operating_model=inspect_operating_model(q)[0];m.identity_checksum=request_digest(q.business_identity)
    return prepare_landing_input(*reseal(m,q))


def reasons_input():
    return prepare_reasons_input(*landing_input())


@pytest.mark.parametrize('gbp,landing,reasons,version',[(a,b,c,17 if a and b and c else 16 if a and b else 15 if a else 14) for a in (False,True) for b in (False,True) for c in (False,True)])
def test_flag_combinations_keep_previous_selection_and_warn(caplog,gbp,landing,reasons,version):
    assert select_reasons_ruleset(gbp,landing,reasons)==f'v22_capability_public_v{version}'
    assert any(getattr(r,'configuration_warning','')=='reasons_requires_gbp_and_landing' for r in caplog.records)==(reasons and not(gbp and landing))


def test_projection_is_evidence_bound_inference_deterministic_and_never_calls_providers():
    m,q=landing_input();old=build_public_capability_report(m,q);before=canonical_json_bytes(old);input_before=canonical_json_bytes(q)
    with patch('httpx.AsyncClient.send',side_effect=AssertionError('NO_PROVIDER')) as provider:
        nm,nq=prepare_reasons_input(m,q);new=build_public_capability_report(nm,nq)
        assert canonical_json_bytes(new)==canonical_json_bytes(build_public_capability_report(nm,nq))
    provider.assert_not_called();assert canonical_json_bytes(q)==input_before
    assert canonical_json_bytes(build_public_capability_report(m,q))==before
    assert [x.metric for x in new.possible_reasons.items]==['rating','review_count']
    for item in new.possible_reasons.items:
        assert item.classification=='inference' and len(item.competitor_indices)>=2 and len(item.basis_refs)>=4
        assert 'may be one contributing factor' in item.customer_text
        assert not any(s in item.customer_text for s in ('/payload/','rank_source','v22_'))
    assert new.findings==old.findings and new.actions==old.actions and new.evidence==old.evidence
    assert new.gbp_comparison==old.gbp_comparison and new.landing_page_comparison==old.landing_page_comparison


@pytest.mark.parametrize('kind',['unchecked','one_competitor','opposite','lower_bound','time_gap','missing_refs'])
def test_absent_or_ineligible_basis_never_becomes_reason(kind):
    m,q=landing_input();report=build_public_capability_report(m,q);g=deepcopy(report.gbp_comparison)
    for key in ('rating','review_count'):
        if kind=='unchecked':
            g.rows[0].metrics[key]=g.rows[0].metrics[key].model_copy(update={'status':'not_checked','value':None})
        for i,row in enumerate(g.rows[1:],1):
            metric=row.metrics[key]
            if kind=='one_competitor' and i>1:metric.status='not_checked';metric.value=None
            if kind=='opposite':metric.value=0.0
            if kind=='lower_bound':metric.lower_bound=True
            if kind=='time_gap':
                from datetime import datetime
                metric.sampled_at=(datetime.fromisoformat(g.rows[0].metrics[key].sampled_at)+timedelta(hours=24,seconds=1)).isoformat()
            if kind=='missing_refs':metric.source_refs=[]
    g.differences=compare_rows(g.rows)
    value=build_possible_reasons(g,report.landing_page_comparison)
    assert value.status=='not_assessed' and value.items==[]


def test_forged_copy_basis_and_historical_module_are_rejected():
    m,q=reasons_input();report=build_public_capability_report(m,q);raw=report.model_dump(mode='json')
    for key in ('customer_text','basis_refs','competitor_indices'):
        bad=deepcopy(raw);item=bad['possible_reasons']['items'][0]
        item[key]='wrong' if key=='customer_text' else ['/payload/not-real'] if key=='basis_refs' else [1,2]
        with pytest.raises(ValueError):CapabilityReport.model_validate_json(canonical_json_bytes(bad))
    old=build_public_capability_report(*landing_input()).model_dump(mode='json');old['possible_reasons']=raw['possible_reasons']
    with pytest.raises(ValueError):CapabilityReport.model_validate_json(canonical_json_bytes(old))


def test_twenty_four_hours_boundary_preserves_eligibility():
    report=build_public_capability_report(*landing_input());g=deepcopy(report.gbp_comparison)
    from datetime import datetime
    for key in ('rating','review_count'):
        for row in g.rows[1:]:row.metrics[key].sampled_at=(datetime.fromisoformat(g.rows[0].metrics[key].sampled_at)+timedelta(hours=24)).isoformat()
    g.differences=compare_rows(g.rows)
    assert len(build_possible_reasons(g,report.landing_page_comparison).items)==2
