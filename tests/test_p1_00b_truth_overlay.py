"""Approved labels are opt-in and cannot change historical truth or sample outputs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import eval_gold
from test_p0_08_gold import GOLD, ROOT, deny_network
from support.private_gold import require_private_gold_inputs

OVERLAY = ROOT / 'tests/fixtures/p1-00b-hours/confirmed-truth-overlay.json'
RULESET = 'v22_capability_public_v11'


def test_overlay_contains_only_the_two_owner_approved_exact_targets():
    value = eval_gold.load_truth_overlay(OVERLAY, ruleset=RULESET, root=GOLD)
    targets = eval_gold.read_json(OVERLAY.parent/'evaluation-targets.json')['targets']
    selected = {c['site_id']: c for c in value['checks']}
    assert set(selected) == {'all-star-plumbing-fresno', 'vision-tech-swansea'}
    for target in targets:
        if target['site_id'] in selected:
            check = selected[target['site_id']]
            assert check['target'] == target['target']
            assert check['rule_id'] == target['rule_id']
            assert check['rule_version'] == target['rule_version']
            assert check['expected_triggered'] is True
            assert check['reviewed_by'] == value['reviewed_by'] == '负责人确认（2026-11-04）'


@pytest.mark.parametrize('mutation', ['version', 'reviewer', 'duplicate', 'unknown_site', 'non_boolean', 'missing_target'])
def test_invalid_or_cross_version_overlay_is_rejected(tmp_path, mutation):
    value = eval_gold.read_json(OVERLAY)
    if mutation == 'version': value['ruleset_version'] = 'v22_capability_public_v10'
    if mutation == 'reviewer': value['reviewed_by'] = ''
    if mutation == 'duplicate': value['checks'].append(deepcopy(value['checks'][0]))
    if mutation == 'unknown_site': value['checks'][0]['site_id'] = 'unknown'
    if mutation == 'non_boolean': value['checks'][0]['expected_triggered'] = 'true'
    if mutation == 'missing_target': value['checks'][0]['target'] = None
    path = tmp_path/'overlay.json'; path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        eval_gold.load_truth_overlay(path, ruleset=RULESET, root=GOLD)
    with pytest.raises(ValueError, match='version'):
        eval_gold.load_truth_overlay(OVERLAY, ruleset='v22_capability_public_v10', root=GOLD)


def test_overlay_never_silently_overwrites_an_existing_label():
    value = eval_gold.read_json(OVERLAY)
    truth = eval_gold.read_json(GOLD/'all-star-plumbing-fresno/truth.json')
    truth['checks'].append(deepcopy(value['checks'][0]))
    before = deepcopy(truth)
    with pytest.raises(ValueError): eval_gold.apply_truth_overlay(truth, value)
    assert truth == before


@pytest.mark.parametrize('mutation', ['target', 'version', 'not_checked', 'unavailable'])
def test_unmatched_or_unchecked_overlay_never_counts_as_correct_or_missed(mutation):
    check = deepcopy(eval_gold.read_json(OVERLAY)['checks'][0])
    evaluation = {k:deepcopy(check[k]) for k in ('rule_id', 'rule_version', 'target')}
    evaluation.update(finding_id='finding', state='triggered', reason='value_mismatch')
    findings = [dict(id='finding', rule_id=check['rule_id'], status='confirmed')]
    if mutation == 'target': check['target']['snapshot_id'] = '00000000-0000-0000-0000-000000000001'
    if mutation == 'version': check['rule_version'] = '2.0.0'
    if mutation == 'not_checked':
        evaluation.update(state='not_checked', finding_id=None, reason='hours_not_comparable'); findings = []
    if mutation == 'unavailable': evaluation = None; findings = []
    rows = eval_gold.compare_findings({'checks':[check]}, findings, [evaluation] if evaluation else [],
                                     unavailable='generation_input_missing' if mutation == 'unavailable' else None)
    assert all(r['verdict'] == '无法判断' for r in rows)
    assert eval_gold.finding_metrics(rows)['judged_generated_findings'] == 0
    assert eval_gold.finding_metrics(rows)['false_negatives'] == 0


def test_cli_enables_overlay_only_with_explicit_option(monkeypatch, capsys):
    calls = []
    def evaluate(*args, **kwargs):
        calls.append(kwargs); return {'errors': []}
    monkeypatch.setattr(eval_gold, 'evaluate_all', evaluate)
    assert eval_gold.main(['--ruleset', RULESET]) == 0
    assert 'truth_overlay' not in calls[-1]
    assert eval_gold.main(['--ruleset', RULESET, '--truth-overlay', str(OVERLAY)]) == 0
    assert calls[-1]['truth_overlay'] == OVERLAY
    capsys.readouterr()


def test_six_sites_score_approved_hours_only_when_overlay_is_explicit(monkeypatch):
    require_private_gold_inputs(GOLD / 'nova-service', ROOT.parent / '.release-private/2026-10-29')
    deny_network(monkeypatch)
    before = {p: p.read_bytes() for p in GOLD.glob('*/truth.json')}
    # Reuse identical generated reports for the second scoring pass, with no extra IO.
    generate = eval_gold.generate
    generated = {}
    def cached(descriptor, directory, private_root, **kwargs):
        key = directory.name
        if key not in generated: generated[key] = generate(descriptor, directory, private_root, **kwargs)
        return generated[key]
    monkeypatch.setattr(eval_gold, 'generate', cached)
    plain = eval_gold.evaluate_all(GOLD, ROOT.parent/'.release-private/2026-10-29', ruleset=RULESET)
    labelled = eval_gold.evaluate_all(GOLD, ROOT.parent/'.release-private/2026-10-29', ruleset=RULESET, truth_overlay=OVERLAY)
    assert 'truth_overlay' not in plain
    assert labelled['truth_overlay']['selected_check_count'] == 2
    assert labelled['truth_overlay']['sha256'] == hashlib.sha256(OVERLAY.read_bytes()).hexdigest()
    for value, denominator in [(plain, 33), (labelled, 35)]:
        assert len(value['cases']) == 6 and not value['errors'] and not value['summary']['incomplete']
        metrics = value['summary']['finding_metrics']
        assert metrics['false_positives'] == 0
        assert metrics['judged_generated_findings'] == denominator
        assert metrics['false_positive_rate'] == 0
        assert value['summary']['field_metrics']['not_checked_rate'] == 7/26
    for old, new in zip(plain['cases'], labelled['cases']):
        assert old['evaluation_catalog'] == new['evaluation_catalog']
        assert old['action_metrics'] == new['action_metrics']
        for row in new['findings']:
            if row.get('check_id') == 'opening-hours-v11-confirmed':
                assert row['kind'] == 'finding' and row['verdict'] == '正确'
    assert before == {p: p.read_bytes() for p in before}
