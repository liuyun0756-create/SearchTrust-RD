"""V18-only recovery cannot erase visible ambiguity, budgets, or provenance."""
import logging

import pytest

from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.site_business_errors import SiteBusinessError
from app.report_v22.site_business_facts import build_site_business_facts
from app.report_v22.site_business_html import (
    HTML_TOLERANCE_RULESET, ParseBudget, parse_html, visible_text,
)
from app.report_v22.site_business_models import SiteBusinessLimits
from site_business_helpers import business_html, request


def parse(html, ruleset=HTML_TOLERANCE_RULESET, **limits):
    return parse_html(html, '/saved/html', ParseBudget(SiteBusinessLimits(**limits)), ruleset_version=ruleset)


@pytest.mark.parametrize('html,code', [
    ('<div>Saved phone</div></div><p>After</p>', 'unmatched_end_tag_ignored'),
    ('<svg fill="none" fill="red"><text>555-9999</text></svg><p>After</p>', 'ignored_svg_duplicate_attribute'),
    ('<svg><path fill="none" fill="red"/></svg><p>After</p>', 'ignored_svg_duplicate_attribute'),
])
def test_v18_recovers_only_isolated_errors_and_keeps_source_spans(html, code, caplog):
    with caplog.at_level(logging.INFO):
        tree = parse(html)
    assert not tree.dom_failed
    assert [r['code'] for r in tree.recoveries] == [code]
    assert tree.text == html and visible_text(tree.root).endswith('After')
    assert '555-9999' not in visible_text(tree.root)
    p = next(n for n in tree.nodes if n.tag == 'p')
    assert html[p.start:p.end] == '<p>After</p>'
    record = next(r for r in caplog.records if r.msg == 'site_business_html_recovery')
    assert (record.recovery_code, record.recovery_count, record.ruleset_version) == (code, 1, HTML_TOLERANCE_RULESET)
    assert not any(value in caplog.text for value in ('555-9999', 'Saved phone', '/saved/html'))


@pytest.mark.parametrize('version', [*(f'v22_capability_public_v{n}' for n in range(1, 18)), 'v22_capability_public_v999', 'unknown'])
def test_v1_to_v17_keep_default_diagnostics_and_candidate_bytes(version):
    value = request(business_html() + '</div><svg fill="none" fill="red"></svg>')
    before = canonical_json_bytes(value)
    legacy = build_site_business_facts(value, text_patterns=True)
    explicit = build_site_business_facts(value, text_patterns=True, ruleset_version=version)
    assert canonical_json_bytes(explicit) == canonical_json_bytes(legacy)
    assert any(d.code == 'html_parse_failed' for d in explicit.pages[0].diagnostics)
    assert canonical_json_bytes(value) == before


@pytest.mark.parametrize('html', [
    '<div id="a" id="b">Ambiguous</div>',
    '<div hidden id="a" id="b">Still not an SVG</div>',
    '<div><p>Misnested</div>',
    '<div>Unclosed',
    '<svg><g></svg>',
])
def test_visible_duplicates_misnesting_and_unclosed_nodes_still_fail(html):
    assert parse(html).dom_failed


@pytest.mark.parametrize('html,limits', [
    ('a' * 11, {'max_html_bytes_per_page': 10}),
    ('<p>text</p>', {'max_html_nodes_per_page': 1}),
    ('<div><p>text</p></div>', {'max_html_depth': 1}),
])
def test_v18_does_not_raise_resource_budgets(html, limits):
    with pytest.raises(SiteBusinessError, match='LIMIT_EXCEEDED'):
        parse(html, **limits)


def test_jsonld_errors_still_block_fields_after_html_recovery():
    value = request('<script type="application/ld+json">{broken</script></div>')
    result = build_site_business_facts(value, ruleset_version=HTML_TOLERANCE_RULESET)
    assert 'jsonld_parse_failed' in {d.code for d in result.pages[0].diagnostics}
    assert result.pages[0].fields['address'].observation_status == 'not_checked'


def test_repeated_stray_end_tags_have_bounded_diagnostics():
    tree = parse('</div>' * 1000)
    assert not tree.dom_failed
    assert tree.recoveries == [dict(code='unmatched_end_tag_ignored', start=0, end=6, count=1000)]


def test_recovery_changes_no_structured_candidate_or_evidence_bytes():
    value = request(business_html() + '</div><svg fill="none" fill="red"></svg>')
    before = canonical_json_bytes(value)
    old = build_site_business_facts(value, text_patterns=True)
    new = build_site_business_facts(value, text_patterns=True, ruleset_version=HTML_TOLERANCE_RULESET)
    assert not any(d.code == 'html_parse_failed' for d in new.pages[0].diagnostics)
    for field in ('candidates', 'evidence_index', 'source_traces'):
        assert canonical_json_bytes(new.model_dump(mode='json')[field]) == canonical_json_bytes(old.model_dump(mode='json')[field])
    assert canonical_json_bytes(new) == canonical_json_bytes(build_site_business_facts(
        value, text_patterns=True, ruleset_version=HTML_TOLERANCE_RULESET))
    assert canonical_json_bytes(value) == before


def test_findings_pipeline_uses_v18_policy_without_changing_input_bindings():
    from unittest.mock import patch
    from app.report_v22.findings import build_public_findings
    from test_v22_capability_public import inputs
    from test_v22_site_gbp_alignment import exact_html, exact_gbp
    from site_business_helpers import source
    with patch('test_v22_capability_public.sample_input', return_value=exact_gbp()):
        _, value = inputs(business_source=source(exact_html() + '</div>'), ruleset='v22_capability_public_v12')
    before = canonical_json_bytes(value)
    flags = dict(text_patterns=True, reviewed_evidence=True, resource_limits=True, sampling_quality=True, opening_hours=True)
    old = build_public_findings(value, **flags)
    new = build_public_findings(value, **flags, ruleset_version=HTML_TOLERANCE_RULESET)
    old_address = next(c for c in old._contact_alignment.fields if c.field == 'address')
    new_address = next(c for c in new._contact_alignment.fields if c.field == 'address')
    assert old_address.state == 'not_checked'
    assert new_address.state == 'exact_match'
    assert canonical_json_bytes(value) == before


def test_frozen_pr_html_recovery_and_explicit_visit_boundary(tmp_path):
    """Public, committed frozen input; no private export or provider is needed."""
    from pathlib import Path
    from uuid import UUID
    from app.report_v22.capability_public import build_public_capability_report
    from app.report_v22.findings import build_public_findings
    from app.report_v22.identity_input import prepare_identity_input
    from app.report_v22.public_reassessment import reassess_public_report
    from scripts.eval_gold import generate, read_json
    directory = Path(__file__).resolve().parent / 'fixtures/gold/pr-plumbing-fresno'
    report, old, value, *_ = generate(read_json(directory / 'input.json'), directory, tmp_path,
                                     ruleset='v22_capability_public_v12')
    before = canonical_json_bytes(value)
    flags = dict(text_patterns=True, reviewed_evidence=True, resource_limits=True,
                 sampling_quality=True, opening_hours=True)
    fresh = build_public_findings(value, **flags, ruleset_version=HTML_TOLERANCE_RULESET)
    assert {c.field: c.state for c in old._contact_alignment.fields if c.field != 'service_area'} == {
        'business_name': 'not_checked', 'address': 'not_checked', 'phone': 'not_checked'}
    assert {c.field: c.state for c in fresh._contact_alignment.fields if c.field != 'service_area'} == {
        'business_name': 'exact_match', 'address': 'semantic_match', 'phone': 'semantic_match'}
    manifest, v2, *_ = prepare_identity_input(report.input_manifest, value)
    unchecked = build_public_findings(v2, **flags, identity_v2=True, ruleset_version=HTML_TOLERANCE_RULESET)
    address = next(c for c in unchecked._contact_alignment.fields if c.field == 'address')
    assert address.state == 'not_checked' and address.not_checked_reason == 'operating_model_unresolved'
    base = build_public_capability_report(manifest, v2)
    branch = next(k for k in v2.evidence_input.context.customer_public_gbp.entity_keys if k.kind == 'cid')
    _, confirmed, _ = reassess_public_report(base, v2, dict(
        operating_model='hybrid', branch_ref=f'{branch.kind}:{branch.value}', customers_can_visit=True,
        reason='Offline customer-facing regression fixture'), request_id=UUID(int=10011),
        evaluated_at='2026-11-19T00:00:00Z')
    checked = build_public_findings(confirmed, **flags, identity_v2=True, ruleset_version=HTML_TOLERANCE_RULESET)
    assert next(c for c in checked._contact_alignment.fields if c.field == 'address').state == 'semantic_match'
    assert confirmed.business_identity.operating_location.role == 'customer_facing'
    assert canonical_json_bytes(confirmed.evidence_input) == canonical_json_bytes(value.evidence_input)
    assert canonical_json_bytes(value) == before


def test_reserved_v19_inherits_tolerance():
    assert not parse('</div>', ruleset='v22_capability_public_v19').dom_failed


def test_unterminated_end_tag_diagnostic_uses_input_end():
    from app.report_v22.site_business_html import HtmlTree

    tree = HtmlTree('before</div', '/saved/html', ParseBudget(SiteBusinessLimits()),
                    ruleset_version=HTML_TOLERANCE_RULESET)
    tree.feed('before')
    tree.handle_endtag('div')
    assert tree.recoveries[0]['end'] == len(tree.text)
