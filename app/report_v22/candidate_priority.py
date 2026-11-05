"""v12-only qualification and full candidate ordering. Historical ranker is unchanged."""
from copy import deepcopy
from app.report_v22.public_rule_catalog import HTTP, NOINDEX

VERSION = "v22_business_priority_v3"


def qualified_technical(f):
    return (f['rule_id'] in {HTTP, NOINDEX} and f.get('kind') == 'fact'
            and f.get('status') == 'confirmed' and f['confidence'] in {'medium', 'high'}
            and f['interpretation'] == 'supported' and bool(f.get('evidence_ids')))


def prioritize_candidates(inputs, findings, business, checksum):
    from app.report_v22.capability_priority import prioritize, RANK
    # Reuse v2 business/execution prose; mask only unqualified technical rule identity
    # in this temporary ranking view. Frozen findings and candidate bodies stay intact.
    rank_findings = deepcopy(findings)
    for f in rank_findings:
        if f['rule_id'] in {HTTP, NOINDEX} and not qualified_technical(f):
            f['rule_id'] = 'unqualified_technical_observation'
    trace = prioritize(inputs, rank_findings, business, checksum, rules_version='v22_business_priority_v2')
    by_id = {f['id']: f for f in findings}
    conditions = business.get('execution_conditions') or {'capacity':'unknown','publishing_access':'unknown'}
    for e in trace['candidates']:
        linked = [by_id[key] for key in e['input']['finding_ids']]
        e['technical_finding_ids'] = sorted(f['id'] for f in linked if qualified_technical(f))
        e['default_visible'] = any(f.get('status') == 'confirmed' and f['confidence'] != 'low' for f in linked)
        e['selected_order'] = None
        e['reason_codes'] = [c for c in e['reason_codes'] if c != 'outside_delivery_budget']
    def key(e):
        return (not e['default_visible'], {'access_indexing':0,'measurement':1,'none':2}[e['prerequisite']],
                {'confirmed_target':0,'confirmed_objective':1,'not_matched':2,'unknown':2}[e['business_match']],
                {'supported':0,'needs_verification':1,'conflicting':2}[e['evidence_support']],
                -RANK[e['severity']], -RANK[e['confidence']],
                int(conditions['capacity']=='limited' and bool(set(e['input']['material_kinds']) & {'title_brief','service_brief','profile_correction'})),
                {'ready_to_use':0,'instructions_only':1,'confirmation_required':2}[e['execution']],e['input']['action_id'])
    pending = list(trace['candidates']); order = []; chosen = []
    while pending:
        available = [e for e in pending if set(e['input']['dependencies']) <= set(order)]
        if not available: raise ValueError('cyclic candidate dependencies')
        e = min(available, key=key); identifier=e['input']['action_id'];order.append(identifier)
        if e['default_visible'] and len(chosen)<3 and set(e['input']['dependencies'])<=set(chosen):
            chosen.append(identifier);e['selected_order']=len(chosen)
        else:
            e['reason_codes'].append('advisor_only_weak_basis' if not e['default_visible'] else
                                     'dependency_not_selected' if not set(e['input']['dependencies']) <= set(chosen) else 'outside_delivery_budget')
        pending.remove(e)
    trace.update(rules_version=VERSION, candidate_order=order, action_order=chosen)
    return trace
