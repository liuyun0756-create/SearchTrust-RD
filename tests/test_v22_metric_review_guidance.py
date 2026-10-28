import pytest
from app.report_v22.metric_review_guidance import metric_review_guidance
from tests.support.metric_review_fixtures import examples, facts

@pytest.mark.parametrize('name,value',list(examples()))
def test_directed_reading_is_bound_non_mutating_and_never_calibrated(name,value):
    before=value.model_dump_json();g=metric_review_guidance(value)
    assert value.model_dump_json()==before
    assert [m.metric for m in g.metrics]==['impressions','clicks','ctr','sessions','key_events']
    assert g.calibration_status=='not_calibrated' and g.effect_conclusion is None and g.causal_attribution is False
    assert g.metrics[4].before is None and g.metrics[4].after is None
    assert g.metrics[4].next_checks[-1]=='verify_event_history'
    assert metric_review_guidance(value)==g


def test_sparse_and_dense_do_not_get_a_sufficiency_pass():
    cases=dict(examples());a=metric_review_guidance(cases['one_count']);b=metric_review_guidance(cases['large_same_percent'])
    assert a.metrics[1].relative_change==b.metrics[1].relative_change==1
    assert a.metrics[1].absolute_change==1 and b.metrics[1].absolute_change==100
    assert a.calibration_status==b.calibration_status=='not_calibrated'
    assert 'ONE_COUNT_DIFFERENCE' in a.metrics[1].notes


def test_per_metric_states_and_next_checks():
    cases={k:metric_review_guidance(v) for k,v in examples()}
    assert cases['zero_denominator'].metrics[2].state=='undefined'
    assert cases['zero_denominator'].metrics[2].percentage_point_change is None
    assert cases['zero_denominator'].metrics[1].state=='descriptive'
    assert cases['equal_ctr'].metrics[2].notes==['UNCHANGED_CTR']
    assert cases['waiting'].metrics[0].next_checks[0]=='wait_for_window'
    assert cases['blocked'].metrics[0].next_checks[0]=='resolve_source_limits'
    assert cases['session_waiting'].metrics[3].state=='waiting'
    assert cases['session_restricted'].metrics[3].before is None
    assert cases['available'].metrics[3].state=='snapshot_only'
    assert cases['session_zero'].metrics[3].relative_change is None
    assert cases['work_unconfirmed'].implementation_state=='not_recorded'
    assert cases['work_unconfirmed'].concurrent_changes=='reported'

@pytest.mark.parametrize('mutation',['counts','delta','ratio','blocked_counts','ga4_delta','ga4_percent','blocked_sessions','source','channel'])
def test_inconsistent_inputs_do_not_produce_reading(mutation):
    raw=dict(examples())['available'].model_dump(mode='json')
    if mutation=='counts':raw['gsc']['metrics'][0]['before']=1.5
    if mutation=='delta':raw['gsc']['metrics'][0]['absolute_change']=999
    if mutation=='ratio':raw['gsc']['metrics'][2]['before']=.99
    if mutation=='blocked_counts':raw['gsc']['state']='waiting'
    if mutation=='ga4_delta':raw['ga4']['difference']=999
    if mutation=='ga4_percent':raw['ga4']['relative_change_percent']='0.00'
    if mutation=='blocked_sessions':raw['ga4'].update(state='withheld',reasons=['SAMPLED'])
    if mutation=='source':raw['gsc']['scope']['source']='ga4'
    if mutation=='channel':raw['ga4']['scope']['channel']='all'
    with pytest.raises(ValueError):metric_review_guidance(facts(raw))
