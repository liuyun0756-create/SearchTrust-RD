import pytest
from app.report_v22.findings import build_public_findings
from app.report_v22.public_rule_catalog import DOMAIN, AHEAD
from findings_helpers import market, request


def test_confirmed_competitors_ahead_use_each_query_and_references():
    result = build_public_findings(request(market()))
    findings = [f for f in result.findings if f.rule_id == AHEAD]
    assert len(findings) == 3
    assert len({f.finding_id for f in findings}) == 3
    assert all(f.evidence_ids and f.comparator_ids for f in findings)
    assert result.site_rollup.counts.checked is None


@pytest.mark.parametrize("entries,triggered", [
    ([("competitor-1.test",1,"provider_position")], True),
    ([(None,1,"provider_position")],False),
    ([],False),
])
def test_domain_absence_is_only_known_url_nonempty_sample(entries,triggered):
    findings = build_public_findings(request(market(entries))).findings
    assert sum(f.rule_id == DOMAIN for f in findings) == (3 if triggered else 0)


def test_ties_and_partial_third_competitor_do_not_clear_unknown_comparison():
    source = market([("example.test",2,"provider_position"),("competitor-1.test",1,"provider_position"),("competitor-2.test",2,"provider_position")])
    result = build_public_findings(request(source))
    assert not any(f.rule_id == AHEAD for f in result.findings)
    assert all(e.state == "not_checked" for e in result.rule_evaluations if e.rule_id == AHEAD)
