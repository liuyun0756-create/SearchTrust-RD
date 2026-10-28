import json
from copy import deepcopy

import pytest

from app.jobs_v22.digest import verified_request_digest
from app.report_v22.comparison_models import ComparisonResult
from app.report_v22.metric_reading import metric_reading
from tests.support.metric_reading_fixtures import count_result, examples


@pytest.mark.parametrize("name,result",list(examples()))
def test_directed_examples_never_upgrade_an_effect_or_rewrite_result(name,result):
    before=result.model_dump_json();guide=metric_reading(result)
    assert guide.comparison_checksum==result.checksum
    assert guide.inference_status=="not_calibrated" and guide.calibrated_threshold is None
    assert guide.effect_conclusion is None and not guide.causal_attribution
    assert result.model_dump_json()==before
    assert metric_reading(result)==guide
    if name in ("missing_source","ga4_withheld"):
        assert guide.state=="unavailable" and not guide.metrics and guide.blocked_reasons==result.reasons


@pytest.mark.parametrize("baseline",[0,1,2,5,10,20,50,100,1000,10000,1000000,2**53-2])
def test_count_resolution_is_exact_and_not_a_universal_pass_threshold(baseline):
    guide=metric_reading(count_result((baseline,baseline+1),(baseline+1,baseline+1)))
    clicks=guide.metrics[0]
    assert clicks.relative_step_pp==(100/baseline if baseline else None)
    assert "ONE_COUNT_DIFFERENCE" in clicks.notes
    assert ("ZERO_BASELINE" in clicks.notes)==(baseline==0)
    assert guide.inference_status=="not_calibrated"


@pytest.mark.parametrize("before_impressions",[1,2,10,100,10000])
@pytest.mark.parametrize("after_impressions",[1,2,10,100,10000])
def test_ctr_resolution_uses_each_own_denominator(before_impressions,after_impressions):
    rate=metric_reading(count_result((1,before_impressions),(1,after_impressions))).metrics[2]
    assert (rate.before_denominator,rate.after_denominator)==(before_impressions,after_impressions)
    assert rate.before_rate_step_pp==100/before_impressions and rate.after_rate_step_pp==100/after_impressions
    assert ("UNCHANGED_CTR" in rate.notes)==(before_impressions==after_impressions)


def test_scaling_preserves_percentage_but_changes_count_resolution():
    small=metric_reading(count_result((1,100),(2,100)))
    large=metric_reading(count_result((100,10000),(200,10000)))
    assert small.metrics[0].relative_step_pp==100
    assert large.metrics[0].relative_step_pp==1
    assert "ONE_COUNT_DIFFERENCE" in small.metrics[0].notes and not large.metrics[0].notes
    assert small.inference_status==large.inference_status=="not_calibrated"


@pytest.mark.parametrize("change",["fraction","negative","too_large","clicks_exceed_impressions","unit","absolute","relative","ctr","percentage_points","reason","state","missing_value"])
def test_resigned_inconsistent_arithmetic_fails_closed(change):
    raw=count_result((10,100),(20,200)).model_dump(mode="json");m=raw["metrics"]
    if change=="fraction":m[0]["before"]=1.5
    elif change=="negative":m[0]["before"]=-1
    elif change=="too_large":m[1]["before"]=2**53
    elif change=="clicks_exceed_impressions":m[0]["before"]=101
    elif change=="unit":m[0]["unit"]="ratio"
    elif change=="absolute":m[0]["absolute_change"]=99
    elif change=="relative":m[0]["relative_change"]=.01
    elif change=="ctr":m[2]["before"]=.9
    elif change=="percentage_points":m[2]["percentage_point_change"]=99
    elif change=="reason":m[0]["reasons"]=["ZERO_BASELINE_NO_RELATIVE_CHANGE"]
    elif change=="state":m[0]["state"]="insufficient"
    else:m[0]["after"]=None
    raw["checksum"]=verified_request_digest({k:v for k,v in raw.items() if k!="checksum"})
    result=ComparisonResult.model_validate_json(json.dumps(raw))
    with pytest.raises(ValueError):metric_reading(result)


def test_mutated_model_instance_is_revalidated():
    result=deepcopy(count_result((1,100),(2,100)));result.metrics[0].before=9
    with pytest.raises(ValueError):metric_reading(result)
