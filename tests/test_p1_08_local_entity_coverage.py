"""Local coverage is bounded to the saved target city and selected website pages."""
from copy import deepcopy
from hashlib import sha256
from unittest.mock import patch

import pytest

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.capability import CapabilityReport
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.local_entity_coverage import (
    prepare_local_entity_input, select_local_entity_ruleset,
)
from app.report_v22.operating_model import inspect_operating_model
from findings_helpers import seal
from test_p1_01_keyword_visibility import reseal
from test_p1_04_comparison_reasons import reasons_input


def local_entity_input():
    manifest, request = reasons_input()
    i = next(i for i, s in enumerate(request.evidence_input.sources) if s.kind == 'site')
    site = request.evidence_input.sources[i]
    index = next(i for i,p in enumerate(site.payload.selected_pages) if p.deep_snapshot)
    deep = site.payload.selected_pages[index].deep_snapshot
    request.evidence_input.context.target_market.city = 'Austin'
    manifest.target_market = request.evidence_input.context.target_market
    title = 'Plumbing services in Austin'
    body = 'We provide plumbing repairs in Austin.'
    deep.html += f'<p>{body}</p>'
    deep.title, deep.text = title, f'{deep.text} {body}'
    deep.content_checksum = 'sha256:' + sha256(deep.html.encode()).hexdigest()
    deep.response_bytes = len(deep.html.encode())
    request.evidence_input.sources[i] = seal(site)
    request.business_identity.operating_model = inspect_operating_model(request)[0]
    manifest.identity_checksum = request_digest(request.business_identity)
    return prepare_local_entity_input(*reseal(manifest, request))


@pytest.mark.parametrize('flags,expected', [
    ((True, True, True, True), 18), ((True, True, True, False), 17),
    ((True, False, True, True), 15), ((True, True, False, True), 16),
    ((False, True, True, True), 14),
])
def test_requires_all_predecessor_flags(caplog, flags, expected):
    assert select_local_entity_ruleset(*flags) == f'v22_capability_public_v{expected}'
    assert any(getattr(r, 'configuration_warning', None) == 'local_entity_requires_comparisons'
               for r in caplog.records) == (flags[-1] and expected != 18)


def test_complete_v18_contract_is_deterministic_and_source_bound():
    manifest, request = local_entity_input()
    before = canonical_json_bytes(request)
    with patch('httpx.AsyncClient.send', side_effect=AssertionError('NO_PROVIDER')) as provider:
        report = build_public_capability_report(manifest, request)
        assert canonical_json_bytes(report) == canonical_json_bytes(build_public_capability_report(manifest, request))
    provider.assert_not_called()
    assert report.schema_version == '2.2.13' and report.input_manifest.schema_version == 'v22_analysis_input_v8'
    assert report.possible_reasons and report.landing_page_comparison and report.keyword_visibility
    assert report.local_entity_coverage.status == 'present'
    assert {e.location for e in report.local_entity_coverage.evidence} >= {'title', 'body'}
    assert canonical_json_bytes(request) == before
    raw = report.model_dump(mode='json')
    for field, value in [('status', 'not_detected'), ('target_city', 'Elsewhere'), ('archive_headings_only', True)]:
        bad = deepcopy(raw)
        bad['local_entity_coverage'][field] = value
        with pytest.raises(ValueError):
            CapabilityReport.model_validate_json(canonical_json_bytes(bad))
    bad = deepcopy(raw)
    bad['local_entity_coverage']['evidence'][0]['snapshot_id'] = '00000000-0000-0000-0000-000000000001'
    with pytest.raises(ValueError):
        CapabilityReport.model_validate_json(canonical_json_bytes(bad))


def test_v17_rejects_module_and_serializes_without_it():
    old = build_public_capability_report(*reasons_input()).model_dump(mode='json')
    assert 'local_entity_coverage' not in old
    with pytest.raises(ValueError):
        CapabilityReport.model_validate_json(canonical_json_bytes({**old, 'local_entity_coverage': None}))


@pytest.mark.parametrize('markup,title,status', [
    ('<p>Serving Austin residents.</p>', None, 'weak_position'),
    ('<footer><p>Austin, TX</p><p>Austin, TX</p></footer>', None, 'weak_position'),
    ('<p hidden>Austin</p><p>Serving Dallas.</p>', None, 'not_detected'),
    ('<div><p>Incomplete area text</div>', None, 'not_checked'),
    ('<p>Austin Plumbing</p>', 'Austin Plumbing Reviews', 'not_detected'),
    ('<p>Serving Dallas.</p>', 'Austin Plumbing | Service in Austin', 'present'),
])
def test_position_uses_only_supported_non_brand_mentions(markup, title, status):
    from app.report_v22.local_entity_coverage import build_local_entity_coverage

    _, req = local_entity_input()
    req.business_identity.business_name = 'Austin Plumbing'
    site = next(s for s in req.evidence_input.sources if s.kind == 'site')
    page = next(p for p in site.payload.selected_pages if p.deep_snapshot)
    site.payload.selected_pages = [page]
    deep = page.deep_snapshot
    deep.html, deep.title, deep.h1 = markup, title, None
    deep.text = ' '.join((markup.replace('<footer>', '').replace('</footer>', '')
                          .replace('<p>', '').replace('</p>', ' ')).split())
    result = build_local_entity_coverage(req)
    assert result.status == status
    assert sum(e.location == 'footer' for e in result.evidence) <= 1
