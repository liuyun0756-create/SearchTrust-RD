"""Directed synthetic facts, not observations or empirical calibration."""
import json
from copy import deepcopy
from app.report_v22.review_projection import build_review_projection
from app.report_v22.review_projection_models import ActionFacts
from app.report_v22.metric_reading import metric_reading
from tests.support.metric_reading_fixtures import count_result
from tests.support.review_projection_fixtures import bundle_raw


def facts(raw):
    return ActionFacts.model_validate_json(json.dumps(raw))


def examples():
    base=build_review_projection(bundle_raw()).seo_actions[0].facts.model_dump(mode='json')
    yield 'available', facts(base)
    for name,before,after in [('one_count',(1,100),(2,100)),('large_same_percent',(100,10000),(200,10000)),
                              ('zero_baseline',(0,100),(4,120)),('zero_denominator',(0,0),(4,100)),
                              ('equal_ctr',(10,200),(40,800)),('unchanged',(20,400),(20,400)),
                              ('falling',(20,400),(10,400)),('tiny_change',(1000000,100000000),(1000001,100000000))]:
        raw=deepcopy(base);r=count_result(before,after)
        raw['gsc']['metrics']=[m.model_dump(mode='json') for m in r.metrics]
        raw['gsc']['reading']=[m.model_dump(mode='json') for m in metric_reading(r).metrics]
        yield name,facts(raw)
    raw=deepcopy(base);raw.update(gsc=None,ga4=None);yield 'not_selected',facts(raw)
    for name,state,reason in [('waiting','waiting','AFTER_WINDOW_NOT_ELAPSED'),('blocked','not_comparable','SOURCE_BINDING_CHANGED')]:
        raw=deepcopy(base);raw['gsc'].update(state=state,reasons=[reason],metrics=[],reading=[]);yield name,facts(raw)
    for name,reason in [('session_waiting','WINDOW_NOT_ELAPSED'),('session_restricted','SAMPLED')]:
        raw=deepcopy(base);raw['ga4'].update(state='withheld',reasons=[reason],before=None,after=None,difference=None,relative_change_percent=None)
        yield name,facts(raw)
    raw=deepcopy(base);raw['gsc']['concurrent_changes']='reported';raw['recorded_work'].update(state='not_recorded',implemented_on=None,timezone=None)
    yield 'work_unconfirmed',facts(raw)
    raw=deepcopy(base);raw['ga4'].update(before=0,after=1,difference=1,relative_change_percent=None)
    yield 'session_zero',facts(raw)
