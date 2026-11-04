"""Offline integrity checks for the initial P0-08 captures; no truth is inferred."""
import hashlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "tests/fixtures/gold"
SITE_IDS = (
    "nova-service", "pr-plumbing-fresno", "all-star-plumbing-fresno",
    "vision-tech-swansea", "seamless-gutters-pensacola", "volcanoes-hiking-guides",
)


@pytest.mark.parametrize("site_id", SITE_IDS)
def test_saved_inputs_match_recorded_checksums(site_id):
    directory = GOLD / site_id
    descriptor = json.loads((directory / "input.json").read_text())
    assert descriptor["site_id"] == site_id
    assert descriptor["snapshot_files"]
    for reference in descriptor["snapshot_files"]:
        assert reference["base"] in {"case", "private"}
        base = directory if reference["base"] == "case" else ROOT.parent / ".release-private/2026-10-29"
        path = base / reference["path"]
        assert path.resolve().is_relative_to(base.resolve())
        if reference["base"] == "private" and not path.is_file():
            pytest.skip("Nova frozen private inputs unavailable; replay readiness is unknown")
        assert hashlib.sha256(path.read_bytes()).hexdigest() == reference["sha256"]
    if site_id != "nova-service":
        capture_path = directory / ("capture-retry.json" if site_id == "volcanoes-hiking-guides" else "capture.json")
        capture = json.loads(capture_path.read_text())
        body = (directory / capture["file"]).read_bytes()
        assert capture["status"] == "captured"
        assert capture["http_status"] == 200
        assert capture["requested_url"] == descriptor.get("saved_page_url", descriptor["site_url"])
        assert len(body) == capture["response_bytes"]
        assert hashlib.sha256(body).hexdigest() == capture["sha256"]


def test_vision_copy_preserves_original_bytes_and_volcanoes_retains_failed_attempt():
    assert (GOLD / "vision-tech-swansea/page.html").read_bytes() == (
        ROOT / "tests/fixtures/p0_03/visiontech-swansea.html"
    ).read_bytes()
    failed = json.loads((GOLD / "volcanoes-hiking-guides/capture.json").read_text())
    assert failed["status"] == "unavailable"
    assert failed["reason"] == "response_too_large"


@pytest.mark.parametrize("site_id", SITE_IDS)
def test_truth_template_has_required_annotation_dimensions(site_id):
    truth = json.loads((GOLD / site_id / "truth.json").read_text())
    assert truth["schema_version"] == "gold_truth_v1"
    assert truth["site_id"] == site_id
    for group in ("site_fields", "gbp_fields"):
        assert {"business_name", "address", "phone", "opening_hours", "service_area"} <= truth[group].keys()
        for field in truth[group].values():
            assert {"values", "evidence_refs"} <= field.keys()
    assert {"operating_model", "local_entities", "trust_claims", "checks"} <= truth.keys()
    checks = {check["id"] for check in truth["checks"]}
    assert len(checks) == len(truth["checks"])
    if site_id == "vision-tech-swansea":
        assert {"phone-presence", "address-presence", "opening-hours-presence"} <= checks
    if site_id == "volcanoes-hiking-guides":
        assert {"business-category", "address-operating-model", "rating-link-destination", "local-content"} <= checks
    if site_id == "seamless-gutters-pensacola":
        assert {"phone-false-absence", "address-false-absence", "opening_hours-false-absence",
                "service_area-false-absence", "service-page-context", "client-weak-actions"} <= checks
        assert "feedback-pending" not in checks


def blank_truth():
    return json.loads((GOLD / "nova-service/truth.json").read_text())


def deny_network(monkeypatch):
    import socket
    def forbidden(*args, **kwargs):
        raise AssertionError("Gold evaluation must not access the network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def synthetic_case(tmp_path):
    """Deliberately hand-labelled synthetic errors, never real-site truth."""
    from app.jobs_v22.digest import canonical_json_bytes
    from app.report_v22.findings import build_public_findings
    from app.report_v22.public_rule_catalog import HTTP, NOINDEX, TITLE
    from test_v22_capability_public import inputs
    manifest, request = inputs([{"status_code": 503}, {"title": "Same"}, {"title": "Same"}])
    result = build_public_findings(request, text_patterns=True, reviewed_evidence=True)
    truth = blank_truth()
    truth.update(site_id="synthetic", reviewed_by="hand-authored test", reviewed_at="2026-11-03T00:00:00Z")
    # Intentionally expect HTTP not to trigger (FP), the duplicate title to
    # trigger (correct), and an absent noindex to trigger (FN).
    checks = [(HTTP, "triggered", False), (TITLE, "triggered", True), (NOINDEX, "not_triggered", True),
              ("v22_public.gbp_phone_alignment", "not_checked", True)]
    for index, (rule, state, expected) in enumerate(checks):
        evaluation = next(e for e in result.rule_evaluations if e.rule_id == rule and e.state == state)
        truth["checks"].append(dict(id=str(index), rule_id=rule, target=evaluation.target.model_dump(mode="json"),
                                    expected_triggered=expected, evidence_refs=["hand-constructed synthetic expectation"]))
    directory = tmp_path / "synthetic"
    directory.mkdir()
    data = canonical_json_bytes(dict(manifest=manifest.model_dump(mode="json"), request=request.model_dump(mode="json")))
    (directory / "bound.json").write_bytes(data)
    ref = dict(base="case", path="bound.json", sha256=hashlib.sha256(data).hexdigest())
    (directory / "input.json").write_text(json.dumps(dict(schema_version="gold_input_v1", site_id="synthetic",
        site_url="https://example.test/", snapshot_files=[ref], generation=dict(kind="bound_input", input=ref))))
    (directory / "truth.json").write_text(json.dumps(truth))
    return directory, truth


def test_offline_actual_generation_counts_false_positive_and_false_negative(tmp_path, monkeypatch):
    from scripts.eval_gold import evaluate_case
    directory, _ = synthetic_case(tmp_path)
    before = {p: p.read_bytes() for p in directory.iterdir()}
    deny_network(monkeypatch)
    case = evaluate_case(directory, tmp_path)
    metric = case["finding_metrics"]
    assert (metric["false_positives"], metric["false_negatives"], metric["judged_generated_findings"]) == (1, 1, 2)
    assert metric["false_positive_rate"] == 0.5
    assert next(row for row in case["findings"] if row.get("check_id") == "3")["verdict"] == "无法判断"
    assert all(p.read_bytes() == content for p, content in before.items())
    print(json.dumps(metric, ensure_ascii=False, sort_keys=True))


def test_blank_truth_missing_input_and_unimplemented_rules_do_not_score_as_correct(tmp_path):
    from scripts.eval_gold import compare_findings, finding_metrics, evaluate_case
    # P0-09 now supplies real generation input. Keep the missing-input coverage
    # with an explicit missing-input descriptor instead of assuming a real gap.
    directory = tmp_path / "missing"
    directory.mkdir()
    descriptor = json.loads((GOLD / "seamless-gutters-pensacola/input.json").read_text())
    descriptor.update(generation=None, snapshot_files=[])
    (directory / "input.json").write_text(json.dumps(descriptor))
    (directory / "truth.json").write_bytes((GOLD / "seamless-gutters-pensacola/truth.json").read_bytes())
    case = evaluate_case(directory, tmp_path)
    assert case["reason"] == "generation_input_missing"
    assert all(row["reason"] == "未标注" for row in case["findings"] + case["checks"])
    assert case["finding_metrics"]["false_positive_rate"] is None
    assert case["action_metrics"]["visible_actions"] is None
    truth = blank_truth()
    truth["checks"] = [dict(id="unsupported", rule_id=None, target=None, expected_triggered=True)]
    rows = compare_findings(truth, [], [])
    assert rows[0]["verdict"] == "无法判断"
    assert finding_metrics(rows)["false_negatives"] == 0


def test_truth_labels_are_bound_to_exact_target_and_duplicates_are_rejected():
    from copy import deepcopy
    from scripts.eval_gold import compare_findings, validate_truth
    truth = blank_truth()
    target = dict(kind="page", urls=["https://example.test/a"])
    truth["checks"] = [dict(id="a", rule_id="rule", target=target, expected_triggered=False)]
    evaluation = dict(rule_id="rule", target=dict(kind="page", urls=["https://example.test/b"]),
                      state="triggered", finding_id="b", reason="condition_met")
    rows = compare_findings(truth, [dict(id="b", rule_id="rule")], [evaluation])
    assert all(row["verdict"] == "无法判断" for row in rows)
    duplicate = deepcopy(truth["checks"][0]); duplicate["id"] = "another-id"
    truth["checks"].append(duplicate)
    with pytest.raises(ValueError, match="duplicate_rule_target_label"):
        validate_truth(truth, truth["site_id"])


@pytest.mark.parametrize("case,reason", [("vision", "identity_unresolved"), ("empty", "site_content_not_checked")])
def test_unchecked_rate_uses_real_alignment_reason_not_missing_inference(case, reason):
    from app.report_v22.findings import build_public_findings
    from scripts.eval_gold import field_metrics
    from test_p0_03_text_patterns import run
    if case == "vision":
        # Real HTML, explicitly SYNTHETIC GBP/context for this unit test only.
        html = (GOLD / "vision-tech-swansea/page.html").read_text()
        request = run(html, phone="0333 335 5510", address="Unit 9, Parc Gelli Werdd, Cross Hands, Llanelli SA14 6EF", country="GB")[3]
    else:
        request = run("<p>Unrelated text</p>")[3]
    result = build_public_findings(request, text_patterns=True, reviewed_evidence=True)
    truth = blank_truth(); truth["site_fields"]["phone"]["present"] = True
    metric = field_metrics(truth, result)
    assert metric["confirmed_present_fields"] == metric["not_checked_fields"] == 1
    assert metric["not_checked_rate"] == 1.0
    assert metric["not_checked_by_reason"] == {reason: 1}
    unavailable = field_metrics(truth, None, unavailable="generation_input_missing")
    assert unavailable["not_checked_rate"] is None
    assert unavailable["unavailable_present_fields"] == 1


def test_client_resampling_count_preserves_mixed_basis_and_approved_override():
    from types import SimpleNamespace as Obj
    from scripts.eval_gold import action_metrics
    report = Obj(schema_version="2.2.5", findings=[
        Obj(id="strong", status="confirmed", confidence="medium"),
        Obj(id="weak", status="not_detected", confidence="low"),
        Obj(id="unchecked", status="not_checked", confidence="medium")], actions=[
        Obj(id="mixed", title="Recheck", finding_ids=["strong", "weak"]),
        Obj(id="weak", title="Recheck", finding_ids=["weak", "unchecked"]),
        Obj(id="fix", title="Repair then recheck", finding_ids=["strong"])])
    templates = {"mixed": "review_market_visibility", "weak": "review_market_visibility", "fix": "restore_site_access_indexing"}
    result = action_metrics(report, templates)
    assert result["visible_actions"] == 2
    assert result["visible_resampling_actions"] == 1
    assert result["visible_weak_only_actions"] == 0
    assert result["visible_actions_with_any_weak_basis"] == 1
    override = Obj(schema_version="v22_delivery_revision_v2", client_action_ids=["weak"])
    result = action_metrics(report, templates, override)
    assert result["visible_resampling_actions"] == result["visible_weak_only_actions"] == 1
    report.schema_version = "2.2.2"
    assert action_metrics(report, templates)["visible_actions"] == 3
    assert action_metrics(report, templates)["visible_weak_only_actions"] is None
    assert action_metrics(report, {})["visible_resampling_actions"] is None


def test_page_type_and_client_weak_action_checks_wait_for_annotation(tmp_path):
    from scripts.eval_gold import compare_other_checks, generate, read_json, action_metrics
    directory, truth = synthetic_case(tmp_path)
    report, _, request, templates, _, _ = generate(read_json(directory / "input.json"), directory, tmp_path)
    page = next(s.payload.pages[1] for s in request.evidence_input.sources if s.kind == "site")
    truth["checks"] = [dict(id="page", kind="page_type", page_url=str(page.url), expected_page_type=None),
                        dict(id="actions", kind="client_weak_actions", expected_count=None)]
    actions = action_metrics(report, templates)
    rows = compare_other_checks(truth, request, actions)
    assert all(r["reason"] == "未标注" for r in rows)
    truth["checks"][0]["expected_page_type"] = page.page_type
    truth["checks"][1]["expected_count"] = 0
    rows = compare_other_checks(truth, request, actions)
    assert all(r["verdict"] == "符合" for r in rows)
    truth["checks"][0]["expected_page_type"] = "service" if page.page_type != "service" else "home"
    assert compare_other_checks(truth, request, actions)[0]["verdict"] == "不符"


def test_invalid_checksum_is_error_not_clean_zero(tmp_path):
    from scripts.eval_gold import evaluate_all
    directory, _ = synthetic_case(tmp_path)
    (directory / "bound.json").write_text("{}")
    result = evaluate_all(tmp_path, tmp_path)
    assert result["errors"] and result["summary"]["incomplete"]
    assert result["summary"]["finding_metrics"]["false_positive_rate"] is None


def test_six_real_sites_run_offline_without_editing_frozen_inputs(monkeypatch):
    from scripts.eval_gold import evaluate_all
    private = ROOT.parent / ".release-private/2026-10-29"
    if not (private / "nova-v5-report.json").exists():
        pytest.skip("Private Nova replay unavailable")
    deny_network(monkeypatch)
    result = evaluate_all(GOLD, private)
    assert not result["errors"]
    assert len(result["cases"]) == 6
    nova = next(c for c in result["cases"] if c["site_id"] == "nova-service")
    assert nova["frozen_replay_byte_identical"] is True
    assert nova["action_metrics"]["visible_resampling_actions"] == 1
    assert nova["action_metrics"]["visible_weak_only_actions"] == 0
    assert result["summary"]["finding_metrics"]["false_positive_rate"] is None
    assert result["summary"]["field_metrics"]["not_checked_rate"] is None
    assert result["summary"]["action_cases_evaluated"] == 6
    assert result["summary"]["action_cases_unavailable"] == 0
    assert all(c["status"] == "已生成" for c in result["cases"])
    assert result["summary"]["incomplete"] is False
