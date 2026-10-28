"""Owner-only next checks derived from frozen facts, never effect calibration."""
from typing import Literal
from pydantic import Field, FiniteFloat
from app.jobs_v22.digest import verified_request_digest
from app.report_v22.comparison import MAX_SAFE, _metrics
from app.report_v22.comparison_base import StrictModel
from app.report_v22.ga4_session_reading import relative_percent
from app.report_v22.review_projection_models import ActionFacts

RULES = 'v22_metric_review_guidance_v1'
Metric = Literal['impressions', 'clicks', 'ctr', 'sessions', 'key_events']
State = Literal['not_selected', 'waiting', 'withheld', 'undefined', 'descriptive', 'snapshot_only', 'not_included']
Check = Literal['review_demand_mix', 'review_page_queries', 'review_ctr_denominators', 'review_session_measurement',
                'verify_event_history', 'select_source', 'wait_for_window', 'resolve_source_limits', 'obtain_impressions']
Note = Literal['ZERO_BASELINE', 'UNCHANGED_COUNT', 'ONE_COUNT_DIFFERENCE', 'ZERO_IMPRESSIONS', 'UNCHANGED_CTR',
               'PROCESSING_NOT_ATTESTED', 'MEASUREMENT_HISTORY_UNVERIFIED', 'EVENT_VALUES_NOT_INCLUDED']

class MetricReview(StrictModel):
    metric: Metric
    state: State
    before: int | None = Field(ge=0, le=MAX_SAFE)
    after: int | None = Field(ge=0, le=MAX_SAFE)
    before_denominator: int | None = Field(ge=0, le=MAX_SAFE)
    after_denominator: int | None = Field(ge=0, le=MAX_SAFE)
    absolute_change: FiniteFloat | None
    relative_change: FiniteFloat | None
    percentage_point_change: FiniteFloat | None
    notes: list[Note]
    source_reasons: list[str]
    next_checks: list[Check]

class MetricReviewGuidance(StrictModel):
    rules_version: Literal['v22_metric_review_guidance_v1']
    facts_checksum: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')
    action_id: str
    metrics: list[MetricReview] = Field(min_length=5, max_length=5)
    implementation_state: Literal['seo_recorded', 'not_recorded']
    concurrent_changes: Literal['unknown', 'reported', 'none_reported', 'not_available']
    calibration_status: Literal['not_calibrated']
    effect_conclusion: None
    causal_attribution: Literal[False]

NEXT = dict(impressions='review_demand_mix', clicks='review_page_queries', ctr='review_ctr_denominators',
            sessions='review_session_measurement', key_events='verify_event_history')

def _card(metric, state, reasons=()):
    gate = dict(not_selected='select_source', waiting='wait_for_window', withheld='resolve_source_limits', undefined='obtain_impressions').get(state)
    return dict(metric=metric, state=state, before=None, after=None, before_denominator=None, after_denominator=None,
                absolute_change=None, relative_change=None, percentage_point_change=None, notes=[], source_reasons=list(reasons),
                next_checks=([gate] if gate else []) + [NEXT[metric]])

def _count(card, before, after):
    card.update(before=before, after=after, absolute_change=after-before,
                relative_change=(after-before)/before if before else None)
    if before == 0: card['notes'].append('ZERO_BASELINE')
    if before == after: card['notes'].append('UNCHANGED_COUNT')
    elif abs(after-before) == 1: card['notes'].append('ONE_COUNT_DIFFERENCE')

def metric_review_guidance(value: ActionFacts) -> MetricReviewGuidance:
    a = ActionFacts.model_validate_json(value.model_dump_json())
    g, ga = a.gsc, a.ga4
    if g:
        if g.scope.source != 'gsc' or g.scope.channel != 'organic_search' or g.scope.dimension not in ('property','page','query'):
            raise ValueError('unsupported search scope')
        if g.state == 'comparable':
            if len(g.metrics) != 3: raise ValueError('missing metrics')
            values = [getattr(g.metrics[i], p) for i in (0,1) for p in ('before','after')]
            if any(v is None or not float(v).is_integer() or not 0 <= v <= MAX_SAFE for v in values):
                raise ValueError('invalid metric counts')
            cb,ca,ib,ia=map(int,values)
            if cb > ib or ca > ia or [m.model_dump() for m in g.metrics] != _metrics((cb,ib),(ca,ia)):
                raise ValueError('inconsistent search arithmetic')
        elif g.metrics or g.reading:
            raise ValueError('blocked metrics cannot expose values')
    if ga:
        if ga.scope.channel != 'organic_search': raise ValueError('unsupported session scope')
        if ga.state == 'available':
            if (ga.before is None or ga.after is None or ga.reasons or ga.difference != ga.after-ga.before
                    or ga.relative_change_percent != relative_percent(ga.before,ga.after)):
                raise ValueError('inconsistent session arithmetic')
        elif not ga.reasons or any(v is not None for v in (ga.before,ga.after,ga.difference,ga.relative_change_percent)):
            raise ValueError('blocked sessions cannot expose values')
    cards=[]
    for metric in ('impressions','clicks','ctr'):
        state = 'not_selected' if not g else 'descriptive' if g.state=='comparable' else 'waiting' if g.state=='waiting' else 'withheld'
        card=_card(metric,state,g.reasons if g else ())
        if state=='descriptive':
            if metric=='ctr':
                card.update(before=cb,after=ca,before_denominator=ib,after_denominator=ia,percentage_point_change=g.metrics[2].percentage_point_change)
                if not ib or not ia:
                    card.update(state='undefined', notes=['ZERO_IMPRESSIONS'], next_checks=['obtain_impressions',NEXT[metric]])
                elif cb*ia==ca*ib: card['notes'].append('UNCHANGED_CTR')
            else:_count(card,ib if metric=='impressions' else cb,ia if metric=='impressions' else ca)
        cards.append(card)
    state='not_selected' if not ga else 'snapshot_only' if ga.state=='available' else 'waiting' if 'WINDOW_NOT_ELAPSED' in ga.reasons else 'withheld'
    card=_card('sessions',state,ga.reasons if ga else ())
    if ga: card['notes'] += ['PROCESSING_NOT_ATTESTED','MEASUREMENT_HISTORY_UNVERIFIED']
    if state=='snapshot_only':_count(card,ga.before,ga.after)
    cards.append(card)
    card=_card('key_events','not_included' if ga else 'not_selected')
    card['notes']=['EVENT_VALUES_NOT_INCLUDED'];cards.append(card)
    return MetricReviewGuidance(rules_version=RULES,facts_checksum=verified_request_digest(a.model_dump(mode='json')),
        action_id=a.action_id,metrics=cards,implementation_state=a.recorded_work.state,
        concurrent_changes=g.concurrent_changes if g else 'not_available',calibration_status='not_calibrated',effect_conclusion=None,causal_attribution=False)
