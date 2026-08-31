from copy import deepcopy
from datetime import timedelta
from uuid import UUID
import json
import os
from pathlib import Path
import subprocess

import pytest

from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22 import findings as builder
from app.report_v22.evidence_errors import EvidenceError
from app.report_v22.findings_errors import FindingsError
from app.report_v22.public_rule_catalog import GBP_RULES, HTTP, TITLE
from evidence_helpers import first_party_source
from findings_helpers import collection, market, request, site


def test_no_sources_means_unknown_counts_and_no_fake_findings():
    result = builder.build_public_findings(request())
    assert result.findings == [] and result.evidence_result.evidence_index == []
    assert len(result.rule_evaluations) == 28
    assert all(e.state == "not_checked" for e in result.rule_evaluations)
    assert {e.rule_id for e in result.rule_evaluations if e.reason == "customer_public_gbp_missing"} == set(GBP_RULES)
    assert result.site_rollup.counts.model_dump() == dict.fromkeys(["checked", "discovered", "eligible_html", "deep_analyzed"])
    assert result.cluster_rollups == []
    assert len(result.site_rollup.layers) == 8
    assert all(layer.status == "not_checked" and not layer.finding_ids and not layer.evidence_ids for layer in result.site_rollup.layers)


@pytest.mark.parametrize("kind", ["verified", "first_party", "duplicate_kind", "overlapping_domain", "extra", "nan", "coerced_limit"])
def test_invalid_inputs_rejected_before_evidence_build(monkeypatch, kind):
    value = request(site())
    if kind == "verified":
        value.evidence_input.context.report_type = "verified_execution"
    elif kind == "first_party":
        value.evidence_input.sources.append(first_party_source())
    elif kind == "duplicate_kind":
        other = site()
        other.binding.snapshot_id = UUID(int=99)
        other.binding.expires_at = other.binding.fetched_at + timedelta(hours=1)
        value.evidence_input.sources.append(other)
    elif kind == "overlapping_domain":
        value.evidence_input.context.competitors[0].website_url = "https://www.EXAMPLE.test/"
    elif kind == "nan":
        value.evidence_input.context.target_market.latitude = float("nan")
    elif kind == "coerced_limit":
        value = value.model_dump(mode="python")
        value["limits"]["max_findings"] = "10"
    else:
        value = {**value.model_dump(mode="python"), "evidence_index": []}
    monkeypatch.setattr(builder, "build_evidence_index", lambda _: pytest.fail("evidence build must not run"))
    with pytest.raises(FindingsError) as exc:
        builder.build_public_findings(value)
    assert exc.value.error_code == "V22_FINDINGS_INPUT_INVALID"


def test_binding_integrity_error_preserved():
    value = request(site())
    value.evidence_input.sources[0].payload.pages[0].title = "tampered private value"
    with pytest.raises(EvidenceError) as exc:
        builder.build_public_findings(value)
    assert "tampered" not in str(exc.value)
    assert exc.value.error_code.startswith("V22_EVIDENCE_")


def test_source_order_duplicates_and_repeated_build_are_deterministic():
    serp = market()
    value = request(site(), serp, collection(serp))
    before = canonical_json_bytes(value)
    baseline = builder.build_public_findings(value)
    assert canonical_json_bytes(value) == before
    value.evidence_input.sources.reverse()
    value.evidence_input.sources.append(deepcopy(value.evidence_input.sources[0]))
    assert canonical_json_bytes(builder.build_public_findings(value)) == canonical_json_bytes(baseline)


def test_finding_identity_is_stable_across_processes():
    value = request(site([{"status_code": 503}]))
    program = "from app.report_v22.findings_models import PublicFindingsInput; from app.report_v22.findings import build_public_findings; import sys; print(build_public_findings(PublicFindingsInput.model_validate_json(sys.stdin.read())).model_dump_json())"
    outputs = [subprocess.check_output([str(Path('.venv/bin/python').absolute()), "-c", program],
               input=value.model_dump_json().encode(), env={**os.environ, "PYTHONHASHSEED": str(seed)}) for seed in (11, 92)]
    assert outputs[0] == outputs[1]
    assert json.loads(outputs[0])["findings"][0]["finding_id"] == builder.build_public_findings(value).findings[0].finding_id


def test_expired_source_is_not_an_all_clear():
    source = site([{"status_code": 503}])
    source.binding.expires_at = source.binding.fetched_at + timedelta(hours=1)
    result = builder.build_public_findings(request(source))
    assert not result.findings and result.site_rollup.counts.checked is None
    assert all(e.state == "not_checked" for e in result.rule_evaluations)
    assert any(e.reason == "source_ineligible" for e in result.rule_evaluations)


@pytest.mark.parametrize("field,value", [("max_findings", 1), ("max_evaluations", 1), ("max_bytes", 1)])
def test_limits_include_all_output_categories(field, value):
    inputs = request(site([{"status_code": 503}, {"status_code": 404}]))
    setattr(inputs.limits, field, value)
    with pytest.raises(FindingsError) as exc:
        builder.build_public_findings(inputs)
    assert exc.value.error_code == "V22_FINDINGS_LIMIT_EXCEEDED"


def test_byte_limit_includes_rollups_and_metadata_exact_boundary():
    inputs = request()
    expected = builder.build_public_findings(inputs)
    size = len(canonical_json_bytes(expected))
    inputs.limits.max_bytes = size
    assert builder.build_public_findings(inputs) == expected
    inputs.limits.max_bytes = size - 1
    with pytest.raises(FindingsError, match="V22_FINDINGS_LIMIT_EXCEEDED"):
        builder.build_public_findings(inputs)


def test_exact_outcomes_deduplicate_and_conflicting_ids_fail(monkeypatch):
    original = builder.site_findings.evaluate
    def duplicate(view):
        for outcome in original(view):
            yield outcome
            yield deepcopy(outcome)
    value = request(site([{"status_code": 503}]))
    baseline = builder.build_public_findings(value)
    monkeypatch.setattr(builder.site_findings, "evaluate", duplicate)
    assert builder.build_public_findings(value) == baseline
    def conflict(view):
        for outcome in original(view):
            yield outcome
            if outcome.finding:
                second = deepcopy(outcome)
                second.finding.statement = "Different observation under the same ID."
                yield second
    monkeypatch.setattr(builder.site_findings, "evaluate", conflict)
    with pytest.raises(FindingsError, match="V22_FINDINGS_ID_CONFLICT"):
        builder.build_public_findings(value)


@pytest.mark.parametrize("corruption", ["unknown_reference", "wrong_page", "wrong_rule", "wrong_version"])
def test_forged_outcomes_fail_safely(monkeypatch, corruption):
    original = builder.site_findings.evaluate
    def corrupt(view):
        for outcome in original(view):
            if outcome.finding:
                if corruption == "unknown_reference":
                    outcome.finding.evidence_ids = outcome.evaluation.evidence_ids = ["ev_" + "0" * 64]
                elif corruption == "wrong_page":
                    wrong = [view.sources["site"].payload.pages[1].url]
                    outcome.finding.affected_urls = outcome.evaluation.target.urls = wrong
                elif corruption == "wrong_rule":
                    outcome.finding.rule_id = outcome.evaluation.rule_id = "invented"
                else:
                    outcome.finding.rule_version = outcome.evaluation.rule_version = "9.0.0"
            yield outcome
    monkeypatch.setattr(builder.site_findings, "evaluate", corrupt)
    with pytest.raises(FindingsError, match="V22_FINDINGS_REFERENCE_INVALID"):
        builder.build_public_findings(request(site([{"status_code": 503}, {}])))


def test_cluster_rollups_only_associate_actual_page_findings():
    result = builder.build_public_findings(request(site([
        {"title": "Repeated", "page_type": "home"},
        {"title": " repeated ", "page_type": "service_detail"},
        {"status_code": 404, "page_type": "location"},
    ])))
    clusters = {c.page_type: c for c in result.cluster_rollups}
    title = next(f for f in result.findings if f.rule_id == TITLE)
    http = next(f for f in result.findings if f.rule_id == HTTP)
    assert title.finding_id in clusters["home"].finding_ids
    assert title.finding_id in clusters["service_detail"].finding_ids
    assert clusters["location"].finding_ids == [http.finding_id]
    assert clusters["location"].counts.eligible_html == 0
    assert all(c.counts.discovered is None for c in clusters.values())
    assert all(layer.status == "not_checked" and not layer.finding_ids for c in clusters.values() for layer in c.layers)


@pytest.mark.parametrize("corruption", ["wrong_cluster", "semantic_rating", "missing_layer", "orphan_trace"])
def test_rollup_membership_and_trace_integrity_are_verified(monkeypatch, corruption):
    if corruption == "orphan_trace":
        original = builder.build_evidence_index
        def corrupt_evidence(value):
            result = original(value)
            result.source_traces.pop()
            return result
        monkeypatch.setattr(builder, "build_evidence_index", corrupt_evidence)
    else:
        original = builder.build_rollups
        def corrupt_rollup(view, outcomes):
            site_rollup, clusters = original(view, outcomes)
            if corruption == "wrong_cluster":
                clusters[0].finding_ids = []
            elif corruption == "semantic_rating":
                site_rollup.layers[0].status = "good"
            else:
                site_rollup.layers.pop()
            return site_rollup, clusters
        monkeypatch.setattr(builder, "build_rollups", corrupt_rollup)
    with pytest.raises(FindingsError, match="V22_FINDINGS_REFERENCE_INVALID"):
        builder.build_public_findings(request(site([{"status_code": 503}])))


def test_evidence_traversal_order_does_not_change_rule_outputs(monkeypatch):
    value = request(site([{"status_code": 503}]), market())
    baseline = builder.build_public_findings(value)
    original = builder.build_evidence_index
    def reversed_index(value):
        evidence = original(value)
        evidence.evidence_index.reverse()
        evidence.source_traces.reverse()
        return evidence
    monkeypatch.setattr(builder, "build_evidence_index", reversed_index)
    result = builder.build_public_findings(value)
    assert result.findings == baseline.findings
    assert result.rule_evaluations == baseline.rule_evaluations
    assert result.site_rollup == baseline.site_rollup
