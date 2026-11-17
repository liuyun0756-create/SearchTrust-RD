from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import pytest

from app.report_v22.landing_page_comparison import (
    METRICS, LandingMetric, LandingRow, build_landing_comparison, compare_rows,
    extract_content, prepare_landing_input, select_comparison_ruleset,
)
from app.collectors.site_inventory_html import extract_html_structure
from app.collectors.site_inventory_urls import SiteScope
from app.collectors.site_inventory_models import SITE_PAGE_TYPE_ORDER
from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.capability_public import build_public_capability_report
from test_p1_02_gbp_comparison import comparison_input

NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)
SID = UUID('00000000-0000-0000-0000-000000000001')


def landing_input():
    """Independent synthetic v16 fixture; no historical fixture is rewritten."""
    from findings_helpers import seal
    import hashlib
    from test_p1_01_keyword_visibility import reseal
    from app.report_v22.operating_model import inspect_operating_model
    from app.jobs_v22.digest import request_digest
    m, q = comparison_input()
    for i, source in enumerate(q.evidence_input.sources):
        inventories = [source.payload] if source.kind == 'site' else [c.site_inventory for c in source.payload.competitors if c.site_inventory] if source.kind == 'competitor' else []
        for inventory in inventories:
            for selected in inventory.selected_pages:
                d = selected.deep_snapshot
                if not d:
                    continue
                markup = '<html><head><title>Synthetic plumbing services</title></head><body>' + d.text + '<p>Our licensed technicians provide reliable plumbing repairs for homeowners.</p><footer>Contractor License #1234567</footer></body></html>'
                structure = extract_html_structure(markup, page_url=str(d.url), scope=SiteScope.from_root(str(inventory.root_url)))
                d.html, d.text, d.title, d.h1 = markup, structure.visible_text, structure.title, structure.h1
                d.content_checksum = 'sha256:' + hashlib.sha256(markup.encode()).hexdigest()
                d.response_bytes = len(markup.encode())
        if inventories:
            q.evidence_input.sources[i] = seal(source)
    q.business_identity.operating_model = inspect_operating_model(q)[0]
    m.identity_checksum = request_digest(q.business_identity)
    m, q = reseal(m, q)
    return prepare_landing_input(m, q)


def extract(markup, *, title='A plumbing company', name='Example Plumbing'):
    page = '<html><head><title>' + title + '</title></head><body>' + markup + '</body></html>'
    structure = extract_html_structure(page, page_url='https://example.test/', scope=SiteScope.from_root('https://example.test/'))
    deep = SimpleNamespace(html=page, text=structure.visible_text, title=title, h1=structure.h1,
        collected_at=NOW, url='https://example.test/')
    return extract_content(deep, city='Fresno', business_name=name, snapshot_id=SID,
        path='/payload/selected_pages/0/deep_snapshot')


@pytest.mark.parametrize('gbp,landing,expected', [(False, False, 14), (False, True, 14), (True, False, 15), (True, True, 16)])
def test_flag_combinations_and_configuration_warning(caplog, gbp, landing, expected):
    assert select_comparison_ruleset(gbp, landing) == f'v22_capability_public_v{expected}'
    assert ('landing_requires_gbp' in str([getattr(r, 'configuration_warning', '') for r in caplog.records])) == (landing and not gbp)
    assert 'example.test' not in caplog.text


def test_positive_rules_keep_coupons_budgets_and_service_prices_separate():
    result = extract('''<p>Our licensed technicians provide reliable plumbing repairs for homeowners.</p>
        <p>Our service fee: $89 per visit includes the initial plumbing assessment.</p>
        <p>Save $35 OFF plumbing repairs with this coupon for local customers.</p>
        <p>We completed a bathroom project for a customer. Estimated budget: $2,000 included the new fixtures.</p>
        <section><h2>Our Works</h2><p>Browse the completed plumbing installations from our recent local projects.</p><img src="one.jpg"><img src="two.jpg"></section>''')
    for key in ('credentials', 'service_price', 'discount', 'project_budget', 'portfolio'):
        assert result[key], key
    assert len(result['service_price']) == 1
    assert '$89' in result['service_price'][0].quote
    assert all('$2,000' not in e.quote and '$35' not in e.quote for e in result['service_price'])


@pytest.mark.parametrize('markup', [
    '<nav><h2>Our Works</h2><a href="/projects">Projects</a><p>We are licensed and insured and serve Fresno.</p></nav>',
    '<p>Applicants must have a driver license #1234567 and be certified.</p>',
    '<p>You should hire a licensed plumber. We recommend another licensed company.</p>',
    '<p>Our technicians are not licensed or insured for this work.</p>',
    '<div class="testimonial"><p>They asked me to pay $60 more for the repairs.</p></div>',
    '<p>Standing behind our work is important to us.</p>',
    '<img src="case-photo.jpg" alt="licensed Fresno project $99">',
])
def test_navigation_jobs_advice_negation_testimonials_and_image_only_are_not_proof(markup):
    found = extract(markup)
    assert not any(found[key] for key in METRICS)


def test_city_is_one_entity_and_footer_has_provenance_but_brand_only_is_not_local_content():
    found = extract('<h1>Plumbing repairs in Fresno</h1><p>Serving Fresno homes.</p><footer>Fresno, CA 93727</footer>')
    assert len(found['local_entities']) == 1
    assert found['local_entities'][0].location == 'h1'
    assert not extract('<p>Fresno Plumbing</p>', title='Fresno Plumbing', name='Fresno Plumbing')['local_entities']
    assert not extract('<p>Company reviews</p>', title='Fresno Plumbing Reviews', name='Fresno Plumbing & Heating Inc')['local_entities']
    assert len(extract('<footer>Contractor License #424352</footer>')['credentials']) == 1


def test_detailed_case_needs_length_completed_work_and_two_specific_details():
    assert not extract('<p>Our project was completed.</p>')['case_study']
    text = 'We completed this project for our customer at the specified location. The old pipes were replaced and the installation finished. ' * 2
    assert extract('<p>' + text + '</p>')['case_study']


def metric(at=NOW):
    evidence = extract('<p>Our licensed technicians provide reliable plumbing repairs for homeowners.</p>')['credentials'][0]
    return LandingMetric(status='observed', count=1, lower_bound=True, reason='saved_positive', evidence=[evidence.model_copy(update={'sampled_at': at.isoformat()})])


@pytest.mark.parametrize('competitors,hours,reason', [(1, 0, 'insufficient_comparators'), (2, 24, 'no_supported_difference'), (2, 24.0001, 'comparison_time_gap')])
def test_comparison_is_inference_with_two_sources_original_time_gate_and_no_bounded_count_ordering(competitors, hours, reason):
    rows = [LandingRow(subject='customer' if i == 0 else 'competitor', name=f'Company {i}', snapshot_id=SID,
        inventory_path='/payload', eligible_html_count=1, deep_page_count=1,
        page_type_counts={k: int(k == 'home') for k in SITE_PAGE_TYPE_ORDER},
        metrics={k: metric(NOW + timedelta(hours=hours if i else 0)) for k in METRICS}) for i in range(competitors + 1)]
    comparisons = compare_rows(rows)
    assert comparisons[0].reason == 'bounded_counts'
    assert comparisons[1].classification == 'inference' and comparisons[1].reason == reason
    assert comparisons[1].evidence[0].sampled_at == NOW.isoformat()
    assert all(c.status == 'not_checked' for c in comparisons)


def test_partial_absence_stays_unknown_and_single_page_inventory_keeps_positive_observations():
    _, q = comparison_input()
    module = build_landing_comparison(q)
    assert module.rows[0].coverage == 'bounded_sample'
    for row in module.rows:
        for m in row.metrics.values():
            if m.status == 'not_checked':
                assert m.count is None and m.evidence == []


def test_v16_contains_v15_gbp_and_keeps_original_sources_findings_actions_and_old_report_bytes():
    m, q = comparison_input()
    old = build_public_capability_report(m, q)
    before, request_before = canonical_json_bytes(old), canonical_json_bytes(q)
    with patch('httpx.AsyncClient.send', side_effect=AssertionError('NO_PROVIDER_CALL')) as provider:
        new_m, new_q = prepare_landing_input(m, q)
        report = build_public_capability_report(new_m, new_q)
    provider.assert_not_called()
    assert new_m.schema_version == 'v22_analysis_input_v6' and report.schema_version == '2.2.11'
    assert report.gbp_comparison == old.gbp_comparison
    assert report.findings == old.findings and report.actions == old.actions
    assert canonical_json_bytes(q) == canonical_json_bytes(new_q) == request_before
    assert canonical_json_bytes(build_public_capability_report(m, q)) == before
    assert 'landing_page_comparison' not in old.model_dump(mode='json')
    assert canonical_json_bytes(build_public_capability_report(new_m, new_q)) == canonical_json_bytes(report)
