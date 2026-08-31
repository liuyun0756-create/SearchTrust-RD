import pytest

from app.report_v22.findings import build_public_findings
from app.report_v22.public_rule_catalog import HTTP, NOINDEX, TITLE
from findings_helpers import request, site


@pytest.mark.parametrize("status,expected", [(399, None), (400,"medium"), (499,"medium"), (500,"high"), (599,"high")])
def test_http_boundaries(status, expected):
    result = build_public_findings(request(site([{"status_code": status}])))
    findings = [f for f in result.findings if f.rule_id == HTTP]
    assert len(findings) == int(expected is not None)
    if findings:
        assert findings[0].severity == expected
        assert str(status) in findings[0].statement


@pytest.mark.parametrize("tokens,triggered", [(["NOINDEX, follow"],True), (["x-noindex"],False), ([],False)])
def test_only_explicit_noindex(tokens, triggered):
    result = build_public_findings(request(site([{"meta_robots": tokens}])))
    assert any(f.rule_id == NOINDEX for f in result.findings) == triggered


def test_title_group_uses_normalized_text_and_distinct_final_urls():
    result = build_public_findings(request(site([{"title": "Plumbing  Service"}, {"title": "plumbing service"}])))
    found = next(f for f in result.findings if f.rule_id == TITLE)
    assert len(found.affected_urls) == 2
    assert found.classification == "fact" and found.severity == "low"
    assert all(layer.status == "not_checked" for layer in result.site_rollup.layers)


def test_title_alias_and_conflicting_observations_do_not_fake_duplicate_pages():
    for titles in (("Same", "Same"), ("One", "Two")):
        result = build_public_findings(request(site([
            {"title": titles[0]}, {"title": titles[1], "final_url": "https://example.test/"},
        ])))
        assert not any(f.rule_id == TITLE for f in result.findings)
        assert any(e.rule_id == TITLE and e.state == "not_checked" for e in result.rule_evaluations)
