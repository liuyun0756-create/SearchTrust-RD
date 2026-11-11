"""Observation-only facts and wording must not change earlier judgments."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.action_models import PublicActionPlanInput
from app.report_v22.actions import select_public_action_skeletons
from app.report_v22.capability_finding_status import rule_kinds
from app.report_v22.capability_public import _COPY, build_public_capability_report
from app.report_v22.findings import build_public_findings
from app.report_v22.public_action_catalog import RULE_ACTIONS
from app.report_v22.public_presentation import (COMPARISON_GROUP, DOMAIN_GROUP,
    NEUTRAL_ZERO_ACTION_SUMMARY, finish_presentation, group_limits, zero_action_summary)
from app.report_v22.public_rule_catalog import OBSERVED, RULES
from test_v22_capability_public import inputs
from test_p0_04_render_fallback import versioned_report_fixture
from findings_helpers import market, seal

BASELINE = json.loads((Path(__file__).parent / 'fixtures/zero-action/history-digests.json').read_text())


def historical(version, sample):
    if version == 7:
        return versioned_report_fixture(failed=sample == 'render-failed')
    records = ([{'title': 'Plumbing'}] if sample == 'zero' else [{'status_code': 503}]
               if sample == 'http' else [{'title': 'Shared'}, {'title': 'Shared'}])
    manifest, value = inputs(records, search_gap=sample == 'gap', ruleset=f'v22_capability_public_v{version}')
    return manifest, value, build_public_capability_report(manifest, value)


@pytest.mark.parametrize('row', BASELINE['reports'], ids=lambda r: f"v{r['version']}-{r['sample']}")
def test_v1_through_v11_canonical_bytes_match_prechange_baseline(row):
    _, _, report = historical(row['version'], row['sample'])
    raw = canonical_json_bytes(report)
    assert len(raw) == row['bytes']
    assert hashlib.sha256(raw).hexdigest() == row['sha256']
    assert all(f.rule_id != OBSERVED for f in report.findings)


@pytest.mark.parametrize('row', BASELINE['v12_action_fields'], ids=lambda r: r['sample'])
def test_v12_existing_actions_priority_and_action_summary_are_unchanged(row):
    _, _, report = historical(12, row['sample'])
    value = report.model_dump(mode='json')
    selected = {key: value[key] for key in ['summary', 'actions', 'action_state', 'prioritization']}
    assert report.actions
    assert hashlib.sha256(canonical_json_bytes(selected)).hexdigest() == row['sha256']


def three_result_types(*, rows=None):
    source = market(rows or [('example.test', 2, 'provider_position')])
    for run in source.payload.query_runs:
        base = run.results[0]
        for kind in ('organic', 'local_pack'):
            run.results.append(base.model_copy(update={'result_type': kind,
                'record_id': request_digest([run.query, kind]), 'call_id': run.google_call.call_id}))
    counts = Counter(r.result_type for run in source.payload.query_runs for r in run.results)
    for count in source.payload.result_counts:
        count.count = counts[count.result_type]
    return seal(source)


@pytest.mark.parametrize('version', [12, 13])
def test_positive_position_facts_are_registered_bound_and_never_actions(version):
    source = three_result_types()
    with patch('test_v22_capability_public.market', return_value=source):
        manifest, value = inputs([{'title': 'Plumbing'}], ruleset='v22_capability_public_v12')
    if version == 13:
        # Use the existing identity-v2 fixture; install the same bound market
        # before constructing it so source checksums remain real validations.
        from test_p1_05_identity import bound
        from app.report_v22.identity_input import prepare_identity_input
        with patch('test_v22_capability_public.market', return_value=source):
            manifest, value = bound()
        manifest, value, _, _ = prepare_identity_input(manifest, value)
    before = canonical_json_bytes(value)
    report = build_public_capability_report(manifest, value)
    facts = [f for f in report.findings if f.rule_id == OBSERVED]
    assert Counter('maps' if 'saved maps' in f.observed_fact else 'organic' for f in facts) == {'maps': 3, 'organic': 3}
    assert all(f.classification == f.kind == 'fact' and f.status == 'confirmed' for f in facts)
    evidence = {e.id: e for e in report.evidence}
    for fact in facts:
        paths = '\n'.join(evidence[key].field_path for key in fact.evidence_ids)
        assert '/position' in paths and '/rank_source' in paths and '/url' in paths
        assert 'position 2 (rank_source=provider_position)' in fact.observed_fact
        assert not any(fact.id in a.finding_ids for a in (report.action_candidates or report.actions))
    assert canonical_json_bytes(value) == before
    assert 'rank_source' not in report.summary and ' · ' not in report.summary
    assert 'v22_public.' not in report.summary
    assert 'Observed Maps positions:' in report.summary
    assert rule_kinds()[OBSERVED] == RULES[OBSERVED].classification == 'fact'
    assert OBSERVED in _COPY and OBSERVED not in RULE_ACTIONS
    result = build_public_findings(value, text_patterns=True, reviewed_evidence=True,
        resource_limits=True, sampling_quality=True, opening_hours=True,
        identity_v2=version == 13, observed_positions=True)
    skeletons = select_public_action_skeletons(PublicActionPlanInput(
        findings_result=result, planning_date=value.evidence_input.context.evaluated_at.date()), all_candidates=True)
    ids = {f.finding_id for f in result.findings if f.rule_id == OBSERVED}
    assert not any(ids.intersection(a.finding_ids) for a in skeletons)


@pytest.mark.parametrize('rows', [[(None, 2, 'provider_position')], [('other.test', 2, 'provider_position')]])
def test_no_positive_fact_without_an_observed_client_domain(rows):
    with patch('test_v22_capability_public.market', return_value=market(rows)):
        manifest, value = inputs([{'title': 'Plumbing'}], ruleset='v22_capability_public_v12')
    report = build_public_capability_report(manifest, value)
    assert all(f.rule_id != OBSERVED for f in report.findings)
    assert report.summary.startswith('This sample did not identify a supported change to propose.')


def test_mixed_position_sources_are_recorded_without_comparing_or_choosing_a_minimum():
    rows = [('example.test', 2, 'provider_position'), ('example.test', 1, 'response_order')]
    with patch('test_v22_capability_public.market', return_value=market(rows)):
        manifest, value = inputs([{'title': 'Plumbing'}], ruleset='v22_capability_public_v12')
    facts = [f for f in build_public_capability_report(manifest, value).findings if f.rule_id == OBSERVED]
    assert len(facts) == 3
    assert all('positions 1 (rank_source=response_order), 2 (rank_source=provider_position)' in f.observed_fact for f in facts)


def test_limit_groups_retain_every_original_sentence_and_do_not_change_other_checks():
    domain = [f"Domain coverage for 'query {i}' (maps) remains unverified: reason {i}." for i in range(7)]
    comparison = [f"The full competitor-position comparison for 'query {i}' (organic) could not be completed. Limit {i}." for i in range(6)]
    others = [f'Unchanged check {i}.' for i in range(6)]
    original = [*domain, *others, *comparison]
    result = group_limits(original)
    assert len(original) == 19 and len(result) == 8
    assert result[0].startswith(DOMAIN_GROUP) and result[-1].startswith(COMPARISON_GROUP)
    assert all(any(old in new.splitlines() for new in result) for old in original)
    assert result[1:7] == others and group_limits(result) == result
    assert group_limits([others[0], others[0]]) == [others[0]]


def test_summary_reads_position_evidence_instead_of_repeating_technical_prose():
    from app.report_v22.public_presentation import zero_action_summary
    findings = [dict(rule_id=OBSERVED, target='Plumbing Services Fresno · maps · 1,2 · en · mobile',
        observed_fact='Technical prose must not become the summary.', evidence_ids=['position'])]
    evidence = [dict(id='position', field_path='/payload/query_runs/0/results/1/position', value=2)]
    assert zero_action_summary(findings, evidence) == (
        'This sample did not identify a supported change to propose. '
        'Observed Maps positions: Plumbing Services Fresno: 2. '
        'Unverified checks and sample limits are listed below.')


@pytest.mark.parametrize('position', [None, True, '2', 2.0])
def test_summary_without_verified_integer_position_is_neutral_and_observable(position, caplog):
    findings = [dict(rule_id=OBSERVED, target='Private query · maps · 1,2 · en · mobile',
        observed_fact='Observed at position 999 (rank_source=provider_position).',
        evidence_ids=['url', 'status', 'position'])]
    evidence = [dict(id='url', field_path='/results/0/url', value='https://private.test/'),
        dict(id='status', field_path='/call/status', value='succeeded'),
        dict(id='position', field_path='/results/0/position', value=position)]
    before = canonical_json_bytes([findings, evidence])
    assert zero_action_summary(findings, evidence) == NEUTRAL_ZERO_ACTION_SUMMARY
    assert canonical_json_bytes([findings, evidence]) == before
    record, = caplog.records
    assert record.event == 'zero_action_summary_degraded'
    assert record.summary_level == 'neutral_without_positions'
    assert record.skipped_position_findings == 1
    assert record.getMessage() == 'ZERO_ACTION_SUMMARY_DEGRADED'
    assert 'Private query' not in caplog.text and 'private.test' not in caplog.text


def test_summary_skips_only_unavailable_positions_and_keeps_target_order(caplog):
    findings = [dict(rule_id=OBSERVED, target=f'{query} · maps · 1,2 · en · mobile',
        observed_fact='Do not parse position 999.', evidence_ids=[key])
        for query, key in [('Z query', 'z'), ('Missing query', 'missing'), ('A query', 'a')]]
    evidence = [dict(id=key, field_path='/results/0/position', value=value)
        for key, value in [('z', 3), ('a', 2)]]
    assert zero_action_summary(findings, evidence) == (
        'This sample did not identify a supported change to propose. '
        'Observed Maps positions: A query: 2; Z query: 3. '
        'Unverified checks and sample limits are listed below.')
    assert caplog.records[0].summary_level == 'partial_positions'


@pytest.mark.parametrize('version', [12, 13])
def test_trimmed_position_evidence_cannot_veto_report_or_mutate_findings(version, caplog):
    from app.report_v22.resource_limits import select_evidence
    with patch('test_v22_capability_public.market', return_value=market([('example.test', 2, 'provider_position')])):
        if version == 12:
            manifest, value = inputs([{'title': 'Plumbing'}], ruleset='v22_capability_public_v12')
        else:
            from test_p1_05_identity import bound
            from app.report_v22.identity_input import prepare_identity_input
            manifest, value = bound()
            manifest, value, _, _ = prepare_identity_input(manifest, value)
    baseline = build_public_capability_report(manifest, value)
    by_id = {e.id: e for e in baseline.evidence}
    observation_ids = [set(f.evidence_ids) for f in baseline.findings if f.rule_id == OBSERVED]
    before_input = (canonical_json_bytes(manifest), canonical_json_bytes(value))
    captured = []

    def trim(ids, source_kind, limit=20):
        if set(ids) in observation_ids:
            ids = [key for key in ids if any(path.endswith(('/url', '/status'))
                for path in by_id[key].field_path.splitlines())]
            assert ids
        return select_evidence(ids, source_kind, limit)

    def capture(raw, manifest):
        captured.append(deepcopy(raw))
        finish_presentation(raw, manifest)

    with patch('app.report_v22.resource_limits.select_evidence', side_effect=trim), \
            patch('app.report_v22.capability_public.finish_presentation', side_effect=capture):
        report = build_public_capability_report(manifest, value)
    assert report.summary == NEUTRAL_ZERO_ACTION_SUMMARY
    assert report.actions == [] and report.action_state == 'insufficient_basis'
    assert {f.id for f in report.findings} == {f.id for f in baseline.findings}
    expected, = captured
    assert canonical_json_bytes(report.model_dump(mode='json')['evidence']) == canonical_json_bytes(expected['evidence'])
    assert {f.id: f.model_dump(mode='json') for f in report.findings} == {
        f['id']: f for f in expected['findings']}
    assert report.limitations == baseline.limitations
    assert (canonical_json_bytes(manifest), canonical_json_bytes(value)) == before_input
    record, = caplog.records  # Exactly one final-summary marker, no double computation.
    assert record.skipped_position_findings == len(observation_ids)
    assert record.summary_level == 'neutral_without_positions'


@pytest.mark.parametrize('version', [12, 13])
def test_semantic_card_order_is_severity_first_deterministic_and_not_action_priority(version):
    from types import SimpleNamespace
    findings = [dict(id=id, rule_id=rule, severity=severity, kind='fact', status=status,
        target=f'{id} · maps · 1,2 · en · mobile', evidence_ids=['position'])
        for id, rule, severity, status in [
            ('fn-a', 'v22_public.market_site_domain_unobserved', 'low', 'not_detected'),
            ('fn-b', 'other', 'low', 'not_checked'),
            ('fn-c', 'confirmed', 'low', 'confirmed'),
            ('fn-z', OBSERVED, 'low', 'confirmed'),
            ('fn-high', 'unchecked', 'high', 'not_checked'),
            ('fn-y', OBSERVED, 'low', 'confirmed')]]
    raw = dict(findings=findings, evidence=[dict(id='position', value=2, field_path='/position')],
        actions=[], action_candidates=[{'id': 'unchanged'}], prioritization={'action_order': []},
        limitations=['Existing limitation.'])
    expected_summary = zero_action_summary(raw['findings'], raw['evidence'])
    before = deepcopy(raw)
    first, second = deepcopy(raw), deepcopy(raw)
    manifest = SimpleNamespace(ruleset_version=f'v22_capability_public_v{version}')
    finish_presentation(first, manifest)
    finish_presentation(second, manifest)
    assert [f['id'] for f in first['findings']] == ['fn-high', 'fn-y', 'fn-z', 'fn-c', 'fn-a', 'fn-b']
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert first['summary'] == expected_summary
    assert {f['id']: f for f in first['findings']} == {f['id']: f for f in before['findings']}
    for key in ['evidence', 'actions', 'action_candidates', 'prioritization', 'limitations']:
        assert first[key] == before[key]
    for version in range(1, 12):
        historical_raw = deepcopy(before)
        finish_presentation(historical_raw, SimpleNamespace(ruleset_version=f'v22_capability_public_v{version}'))
        assert canonical_json_bytes(historical_raw) == canonical_json_bytes(before)
