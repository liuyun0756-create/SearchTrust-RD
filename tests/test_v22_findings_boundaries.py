from datetime import timedelta
import pytest

from app.report_v22 import findings as builder
from app.report_v22.findings_errors import FindingsError
from app.report_v22.public_rule_catalog import AHEAD, ASSET, DOMAIN, HTTP, NOINDEX, TITLE
from findings_helpers import collection, market, request, seal, site


def evaluations(result, rule, kind=None):
    return [e for e in result.rule_evaluations if e.rule_id == rule and (kind is None or e.target.page_type == kind)]


@pytest.mark.parametrize("changes,eligible", [
    ({"status_code": 200, "content_type": "text/html; charset=utf-8"}, 1),
    ({"status_code": 299, "content_type": "Application/XHTML+XML"}, 1),
    ({"status_code": 300}, 0), ({"status_code": 404}, 0),
    ({"content_type": "application/pdf"}, 0), ({"content_type": "application/octet-stream"}, 0),
])
def test_only_checked_2xx_html_enters_sample_counts(changes, eligible):
    result = builder.build_public_findings(request(site([changes])))
    assert result.site_rollup.counts.eligible_html == eligible
    assert result.cluster_rollups[0].counts.eligible_html == eligible
    assert all(e.state == "not_checked" for e in evaluations(result, ASSET))
    if not eligible:
        assert all(e.state == "not_checked" for e in evaluations(result, NOINDEX))


@pytest.mark.parametrize("status", ["failed", "robots_disallowed"])
def test_unchecked_pages_never_gain_http_or_noindex_findings(status):
    source = site([{}, {"check_status": status, "status_code": None,
                         "error_code": "page_timeout" if status == "failed" else "robots_unavailable"}])
    result = builder.build_public_findings(request(source))
    assert not any(f.rule_id in {HTTP, NOINDEX} for f in result.findings)
    assert sum(e.state == "not_checked" for e in evaluations(result, HTTP)) == 1
    assert result.site_rollup.counts.checked == 1


@pytest.mark.parametrize("third,expected", [(None, "not_checked"), ("home", "not_triggered"), ("service_detail", "triggered")])
def test_asset_third_competitor_missing_does_not_become_clear(third, expected):
    serp = market()
    result = builder.build_public_findings(request(site(), serp, collection(serp, ("service_detail", "home", third))))
    assert evaluations(result, ASSET, "service_detail")[0].state == expected


def test_two_positive_competitors_are_enough_but_missing_third_is_disclosed():
    serp = market()
    result = builder.build_public_findings(request(site(), serp, collection(serp, ("service_detail", "service_detail", None))))
    found = next(f for f in result.findings if f.rule_id == ASSET)
    assert any("lacked an eligible site sample" in note for note in found.missing_data)


@pytest.mark.parametrize("kind", ["service_detail", "service_area", "location"])
def test_positive_client_page_type_needs_no_comparator_for_nontrigger(kind):
    result = builder.build_public_findings(request(site([{"page_type": kind}])))
    assert evaluations(result, ASSET, kind)[0].state == "not_triggered"


def test_outlier_third_inventory_cannot_be_discarded_to_make_a_gap():
    serp = market()
    source = collection(serp)
    old = source.payload.competitors[2].site_inventory
    old.started_at -= timedelta(hours=25)
    old.completed_at -= timedelta(hours=25)
    result = builder.build_public_findings(request(site(), serp, seal(source)))
    assert all(e.reason == "comparison_time_gap" for e in evaluations(result, ASSET))


@pytest.mark.parametrize("ranks,expected", [
    ([1, 2, 3, 4], "not_triggered"), ([2, 1, 3, 4], "not_triggered"),
    ([3, 1, 2, 4], "triggered"), ([4, 1, 2, 3], "triggered"),
    ([2, 1, 2, 2], "not_triggered"),
])
def test_rank_threshold_and_ties(ranks, expected):
    domains = ["example.test", "competitor-1.test", "competitor-2.test", "competitor-3.test"]
    source = market([(domain, rank, "provider_position") for domain, rank in zip(domains, ranks)])
    result = builder.build_public_findings(request(source))
    assert all(e.state == expected for e in evaluations(result, AHEAD) if e.target.result_type == "maps")


@pytest.mark.parametrize("mixed_client", [True, False])
def test_mixed_rank_basis_cannot_create_a_comparison(mixed_client):
    entries = [("example.test", 10, "provider_position"), ("competitor-1.test", 1, "provider_position"),
               ("competitor-2.test", 2, "response_order"), ("competitor-3.test", 3, "response_order")]
    if mixed_client:
        entries.append(("example.test", 5, "response_order"))
    result = builder.build_public_findings(request(market(entries)))
    assert all(e.state == "not_checked" and e.reason == "rank_basis_mismatch" for e in evaluations(result, AHEAD) if e.target.result_type == "maps")


def test_domain_matching_uses_url_not_business_name():
    source = market([("other.test", 1, "provider_position")])
    for run in source.payload.query_runs:
        run.results[0].display_name = "Client Example Business"
    result = builder.build_public_findings(request(seal(source)))
    assert len([f for f in result.findings if f.rule_id == DOMAIN]) == 3
    assert not any(f.rule_id == AHEAD for f in result.findings)


@pytest.mark.parametrize("kind", ["local_pack", "organic"])
def test_google_result_groups_are_independent_and_cannot_borrow_maps(kind):
    source = market()
    for run in source.payload.query_runs:
        for record in run.results:
            record.call_id = run.google_call.call_id
            record.result_type = kind
    source.payload.result_counts[0].count = 0
    next(c for c in source.payload.result_counts if c.result_type == kind).count = 12
    result = builder.build_public_findings(request(seal(source)))
    assert len([f for f in result.findings if f.rule_id == AHEAD]) == 3
    assert all(e.state == "not_checked" for e in evaluations(result, AHEAD) if e.target.result_type != kind)


@pytest.mark.parametrize("field,value", [("result_type", "organic"), ("query", "other"), ("latitude", 1.0), ("device", "desktop"), ("language", "fr"), ("country_code", "CA")])
def test_wrong_market_context_reference_is_rejected(monkeypatch, field, value):
    original = builder.market_findings.evaluate
    def corrupt(view):
        for outcome in original(view):
            if outcome.finding:
                setattr(outcome.evaluation.target, field, value)
                if field == "query":
                    outcome.finding.affected_queries = [value]
            yield outcome
    monkeypatch.setattr(builder.market_findings, "evaluate", corrupt)
    with pytest.raises(FindingsError, match="V22_FINDINGS_REFERENCE_INVALID"):
        builder.build_public_findings(request(market()))


def test_title_missing_is_not_a_clean_duplicate_audit():
    result = builder.build_public_findings(request(site([{"title": None}, {"title": "unique"}])))
    assert not any(f.rule_id == TITLE for f in result.findings)
    assert evaluations(result, TITLE)[0].state == "not_checked"


def test_two_ahead_with_unknown_third_discloses_partial_comparison():
    source = market([("example.test", 10, "provider_position"), ("competitor-1.test", 1, "provider_position"),
                     ("competitor-2.test", 2, "provider_position")])
    result = builder.build_public_findings(request(source))
    found = [f for f in result.findings if f.rule_id == AHEAD]
    assert len(found) == 3
    assert all(any("Only 2 of 3" in note for note in f.missing_data) for f in found)


def test_competitor_404_classification_and_multiple_pages_do_not_count_as_two_sites():
    serp = market()
    source = collection(serp, ("service_detail", "service_detail", None))
    source.payload.competitors[0].site_inventory = site([
        {"page_type": "service_detail"}, {"page_type": "service_detail"},
    ], host="competitor-1.test").payload
    source.payload.competitors[1].site_inventory.pages[0].status_code = 404
    result = builder.build_public_findings(request(site(), serp, seal(source)))
    assert evaluations(result, ASSET, "service_detail")[0].state == "not_checked"


def test_market_collection_binding_mismatch_is_not_a_data_gap():
    from app.report_v22.evidence_errors import EvidenceError
    serp = market()
    source = collection(serp)
    source.payload.market_snapshot_checksum = "sha256:" + "0" * 64
    with pytest.raises(EvidenceError):
        builder.build_public_findings(request(site(), serp, seal(source)))


def test_full_source_limitations_and_lowest_support_confidence_survive(monkeypatch):
    source = site([{"status_code": 503}])
    source.payload.limitations = [f"Synthetic source limitation {index}" for index in range(25)]
    original = builder.build_evidence_index
    def low_confidence(value):
        result = original(value)
        for item in result.evidence_index:
            item.confidence = "low"
        return result
    monkeypatch.setattr(builder, "build_evidence_index", low_confidence)
    result = builder.build_public_findings(request(seal(source)))
    found = next(f for f in result.findings if f.rule_id == HTTP)
    assert found.confidence == "low"
    assert set(source.payload.limitations) <= set(found.missing_data)


def test_long_query_context_keeps_full_identity_without_overlong_scope():
    source = market()
    queries = [str(index) + "q" * 299 for index in range(3)]
    source.payload.queries = queries
    for run, query in zip(source.payload.query_runs, queries):
        run.query = run.maps_call.query = run.google_call.query = query
        for record in run.results:
            record.query = query
    result = builder.build_public_findings(request(seal(source), queries=queries))
    found = [f for f in result.findings if f.rule_id == AHEAD]
    assert len({f.finding_id for f in found}) == 3
    assert {f.affected_queries[0] for f in found} == set(queries)
    assert all(len(f.scope) < 100 for f in found)
