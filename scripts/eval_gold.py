#!/usr/bin/env python3
"""Read-only, offline P0-08 evaluation. Blank truth never means a negative label."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.report_v22.action_models import PublicActionPlanInput
from app.report_v22.actions import select_public_action_skeletons
from app.report_v22.capability import DeliveryRevision, instant
from app.report_v22.capability_finding_status import PUBLIC_RULESET
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.findings import build_public_findings
from app.report_v22.findings_models import PublicFindingsInput, RuleTarget


class InputUnavailable(ValueError):
    pass


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_ref(reference, directory, private_root):
    bases = {"case": directory, "private": private_root}
    base = bases[reference["base"]].resolve()
    path = (base / reference["path"]).resolve()
    if not path.is_relative_to(base):
        raise ValueError("input_path_outside_base")
    if not path.is_file():
        raise InputUnavailable("snapshot_unavailable")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != reference["sha256"]:
        raise ValueError("snapshot_checksum_mismatch")
    return data


def target_key(rule_id, target):
    # Include the full target: different pages, queries or sampling contexts
    # must never borrow each other's human labels.
    normalized = RuleTarget.model_validate_json(canonical_json_bytes(target)).model_dump(mode="json")
    return rule_id, canonical_json_bytes(normalized)


def validate_truth(truth, site_id):
    if truth["schema_version"] != "gold_truth_v1" or truth["site_id"] != site_id:
        raise ValueError("truth_identity_invalid")
    keys, ids = set(), set()
    for field in truth["site_fields"].values():
        if field["present"] is not None and type(field["present"]) is not bool:
            raise ValueError("presence_must_be_boolean_or_null")
    for check in truth["checks"]:
        if check["id"] in ids:
            raise ValueError("duplicate_check_id")
        ids.add(check["id"])
        expected = check.get("expected_triggered")
        if expected is not None and type(expected) is not bool:
            raise ValueError("expectation_must_be_boolean_or_null")
        if check.get("kind", "rule") not in {"rule", "page_type", "client_weak_actions"}:
            raise ValueError("unsupported_check_kind")
        count = check.get("expected_count")
        if count is not None and (type(count) is not int or count < 0):
            raise ValueError("expected_count_must_be_nonnegative_integer_or_null")
        if check.get("rule_id") and check.get("target") is not None:
            key = target_key(check["rule_id"], check["target"])
            if key in keys:
                raise ValueError("duplicate_rule_target_label")
            keys.add(key)


def generate(descriptor, directory, private_root):
    for reference in descriptor["snapshot_files"]:
        read_ref(reference, directory, private_root)
    generation = descriptor.get("generation")
    if generation is None:
        raise InputUnavailable("generation_input_missing")

    def document(ref):
        return json.loads(read_ref(ref, directory, private_root))

    frozen_equal = None
    if generation["kind"] == "nova_frozen_recipe":
        old = document(generation["report"])["payload"]
        snapshots = {row["id"]: row for row in document(generation["snapshots"])}
        request = deepcopy(document(generation["recipe"])["recipe"]["request"])
        for source in request["evidence_input"]["sources"]:
            source["payload"] = snapshots[source["binding"]["snapshot_id"]]["normalized_payload"]
            if source.get("shared_snapshot") is not None:
                source["shared_snapshot"]["snapshot"] = source["payload"]
        frozen_equal = canonical_json_bytes(build_public_capability_report(old["input_manifest"], request)) == canonical_json_bytes(old)
        if not frozen_equal:
            raise ValueError("frozen_replay_changed")
        manifest = deepcopy(old["input_manifest"])
    elif generation["kind"] == "bound_input":
        bound = document(generation["input"])
        manifest, request = deepcopy(bound["manifest"]), bound["request"]
    else:
        raise ValueError("unsupported_generation_kind")

    # Only a new in-memory manifest changes version. No file is ever written.
    manifest["ruleset_version"] = PUBLIC_RULESET
    manifest["input_checksum"] = verified_request_digest({k: v for k, v in manifest.items() if k != "input_checksum"})
    report = build_public_capability_report(manifest, request)
    request = PublicFindingsInput.model_validate_json(canonical_json_bytes(request))
    if str(request.evidence_input.context.site_url) != descriptor["site_url"]:
        raise ValueError("case_site_url_mismatch")
    result = build_public_findings(request, text_patterns=True, reviewed_evidence=True,
                                   rendered_pages=manifest["schema_version"] == "v22_analysis_input_v2")
    skeletons = select_public_action_skeletons(PublicActionPlanInput(
        findings_result=result, planning_date=instant(manifest["evaluated_at"]).date()), all_candidates=True)
    templates = {item.action_id: item.template_key for item in skeletons}
    finding_rules = {item.id: item.rule_id for item in report.findings}
    for action in report.actions:
        if action.id not in templates and all(finding_rules[key] == "v22_capability.service_title_review" for key in action.finding_ids):
            templates[action.id] = "service_title_review"
    revision = None
    if generation.get("delivery_revision"):
        revision = DeliveryRevision.model_validate_json(canonical_json_bytes(document(generation["delivery_revision"])))
        revision.validate_report(report)
    return report, result, request, templates, revision, frozen_equal


def compare_findings(truth, findings, evaluations, *, unavailable=None):
    """Score only exact human-labelled rule/target pairs; never infer a label."""
    by_finding = {item["finding_id"]: item for item in evaluations if item.get("finding_id")}
    by_target = {}
    for item in evaluations:
        key = target_key(item["rule_id"], item["target"])
        if key in by_target:
            raise ValueError("ambiguous_rule_evaluation")
        by_target[key] = item
    labels = {
        target_key(check["rule_id"], check["target"]): check
        for check in truth["checks"] if check.get("kind", "rule") == "rule"
        and check.get("rule_id") and check.get("target") is not None
    }
    rows, consumed = [], set()
    for finding in findings:
        evaluation = by_finding.get(finding["id"])
        check = labels.get(target_key(evaluation["rule_id"], evaluation["target"])) if evaluation else None
        row = dict(kind="finding", finding_id=finding["id"], rule_id=finding["rule_id"],
                   verdict="无法判断", reason="未标注")
        if evaluation:
            row["target"] = evaluation["target"]
        if check:
            consumed.add(check["id"])
            row["check_id"] = check["id"]
            if check.get("expected_triggered") is not None:
                if finding.get("status") == "not_checked" or evaluation["state"] == "not_checked":
                    row["reason"] = evaluation["reason"]
                else:
                    row.update(verdict="正确" if check["expected_triggered"] else "误报", reason="人工标注比对")
        rows.append(row)
    for check in truth["checks"]:
        if check.get("kind", "rule") != "rule" or check["id"] in consumed:
            continue
        row = dict(kind="expected_finding", check_id=check["id"], rule_id=check.get("rule_id"),
                   verdict="无法判断", reason="未标注")
        if check.get("expected_triggered") is not None:
            key = target_key(check["rule_id"], check["target"]) if check.get("rule_id") and check.get("target") is not None else None
            evaluation = by_target.get(key)
            if unavailable:
                row["reason"] = unavailable
            elif evaluation is None:
                row["reason"] = "规则或精确目标未评估"
            elif evaluation["state"] == "not_checked":
                row["reason"] = evaluation["reason"]
            elif evaluation["state"] == "not_triggered":
                row.update(verdict="漏报" if check["expected_triggered"] else "正确", reason="已完成检查，未生成发现")
            else:
                raise ValueError("triggered_finding_missing_from_report")
        rows.append(row)
    return rows


def finding_metrics(rows):
    judged = [r for r in rows if r["kind"] == "finding" and r["verdict"] in {"正确", "误报"}]
    false_positives = sum(r["verdict"] == "误报" for r in judged)
    return dict(verdict_counts=dict(Counter(r["verdict"] for r in rows)),
                false_positives=false_positives, judged_generated_findings=len(judged),
                false_negatives=sum(r["verdict"] == "漏报" for r in rows),
                false_positive_rate=false_positives / len(judged) if judged else None)


def field_metrics(truth, result, *, unavailable=None):
    fields = {f.field: f for f in result._contact_alignment.fields} if result and result._contact_alignment else {}
    evaluations = {e.rule_id: e for e in result.rule_evaluations} if result else {}
    rules = {"business_name": "v22_public.gbp_name_alignment", "address": "v22_public.gbp_address_alignment",
             "phone": "v22_public.gbp_phone_alignment", "service_area": "v22_public.gbp_service_area_alignment"}
    rows = []
    for name, truth_field in truth["site_fields"].items():
        row = dict(field=name, truth_present=truth_field["present"], state=None, reason="未标注")
        if truth_field["present"] is not None:
            if unavailable:
                row["reason"] = unavailable
            elif name in fields:
                row.update(state=fields[name].state, reason=fields[name].not_checked_reason)
            else:
                evaluation = evaluations.get(rules.get(name))
                row.update(state="not_checked", reason=evaluation.reason if evaluation else "semantic_rules_not_implemented")
        rows.append(row)
    return summarize_fields(rows)


def summarize_fields(rows):
    present = [row for row in rows if row["truth_present"] is True]
    unchecked = [row for row in present if row["state"] == "not_checked"]
    unavailable = [row for row in present if row["state"] is None]
    return dict(fields=rows, confirmed_present_fields=len(present), not_checked_fields=len(unchecked),
                unavailable_present_fields=len(unavailable),
                not_checked_rate=len(unchecked) / len(present) if present and not unavailable else None,
                not_checked_by_reason=dict(Counter(row["reason"] for row in unchecked)))


def action_metrics(report, templates, revision=None):
    """Mirror P0-07: all weak -> hidden; any confirmed/non-low -> retained."""
    if report is None:
        return dict(basis="generation_unavailable", visible_actions=None, visible_resampling_actions=None,
                    visible_weak_only_actions=None, visible_actions_with_any_weak_basis=None, actions=[])
    findings = {f.id: f for f in report.findings}
    labelled = report.schema_version == "2.2.5"
    explicit = revision is not None and labelled and revision.schema_version == "v22_delivery_revision_v2"
    rows = []
    for action in report.actions:
        weak = [findings[key].status != "confirmed" or findings[key].confidence == "low" for key in action.finding_ids] if labelled else None
        all_weak = all(weak) if weak is not None else None
        visible = action.id in revision.client_action_ids if explicit else not (labelled and all_weak)
        template = templates.get(action.id)
        rows.append(dict(action_id=action.id, title=action.title, client_visible=visible,
                         template=template, resampling=template == "review_market_visibility" if template else None,
                         weak_only=all_weak, any_weak_basis=any(weak) if weak is not None else None))
    visible = [row for row in rows if row["client_visible"]]
    return dict(basis="explicit_delivery_revision" if explicit else "default_client_visibility",
                visible_actions=len(visible),
                visible_resampling_actions=sum(r["resampling"] for r in visible) if all(r["resampling"] is not None for r in visible) else None,
                visible_weak_only_actions=sum(r["weak_only"] for r in visible) if labelled else None,
                visible_actions_with_any_weak_basis=sum(r["any_weak_basis"] for r in visible) if labelled else None,
                actions=rows)


def compare_other_checks(truth, request, actions, *, unavailable=None):
    rows = []
    for check in truth["checks"]:
        kind = check.get("kind", "rule")
        if kind == "rule":
            continue
        expected = check.get("expected_page_type" if kind == "page_type" else "expected_count")
        row = dict(check_id=check["id"], expected=expected, actual=None, verdict="无法判断", reason="未标注")
        if not unavailable:
            if kind == "client_weak_actions":
                row["actual"] = actions["visible_weak_only_actions"]
            else:
                pages = [page for source in request.evidence_input.sources if source.kind == "site"
                         for page in source.payload.pages if str(page.url) == check["page_url"] and page.check_status == "checked"]
                if len(pages) == 1:
                    row["actual"] = pages[0].page_type
        if expected is not None:
            if row["actual"] is None:
                row["reason"] = unavailable or "未检查"
            else:
                row.update(verdict="符合" if row["actual"] == expected else "不符", reason="人工标注比对（非发现，不计误报率）")
        rows.append(row)
    return rows


def evaluate_case(directory, private_root):
    descriptor = read_json(directory / "input.json")
    truth = read_json(directory / "truth.json")
    validate_truth(truth, descriptor["site_id"])
    report = result = request = revision = frozen_equal = None
    templates, unavailable, evaluations = {}, None, []
    try:
        report, result, request, templates, revision, frozen_equal = generate(descriptor, directory, private_root)
        evaluations = [e.model_dump(mode="json") for e in result.rule_evaluations]
    except InputUnavailable as exc:
        unavailable = str(exc)
    findings = [f.model_dump(mode="json") for f in report.findings] if report else []
    rows = compare_findings(truth, findings, evaluations, unavailable=unavailable)
    actions = action_metrics(report, templates, revision)
    return dict(site_id=descriptor["site_id"], status="无法判断" if unavailable else "已生成",
                reason=unavailable, ruleset=PUBLIC_RULESET, frozen_replay_byte_identical=frozen_equal,
                findings=rows, finding_metrics=finding_metrics(rows),
                field_metrics=field_metrics(truth, result, unavailable=unavailable), action_metrics=actions,
                checks=compare_other_checks(truth, request, actions, unavailable=unavailable),
                evaluation_catalog=evaluations)


def evaluate_all(root, private_root, sites=None):
    cases, errors = [], []
    directories = [root / name for name in sites] if sites else sorted(p.parent for p in root.glob("*/truth.json"))
    if not directories:
        raise ValueError("no_gold_cases")
    for directory in directories:
        try:
            cases.append(evaluate_case(directory, private_root))
        except (ValueError, KeyError, OSError, TypeError) as exc:
            # Keep bad input visible, without dumping private source payloads.
            errors.append(dict(site_id=directory.name, error_type=type(exc).__name__, reason="输入无效，不能评估"))
    fields = [dict(row, site_id=case["site_id"]) for case in cases for row in case["field_metrics"]["fields"]]
    available_actions = [c["action_metrics"] for c in cases if c["action_metrics"]["visible_actions"] is not None]
    totals = {key: sum(a[key] for a in available_actions) if available_actions and all(a[key] is not None for a in available_actions) else None for key in (
        "visible_actions", "visible_resampling_actions", "visible_weak_only_actions", "visible_actions_with_any_weak_basis")}
    return dict(schema_version="gold_evaluation_v1", external_calls=0, cases=cases, errors=errors,
                summary=dict(finding_metrics=finding_metrics([r for c in cases for r in c["findings"]]),
                             field_metrics=summarize_fields(fields), action_totals_available_cases=totals,
                             action_cases_evaluated=len(available_actions),
                             action_cases_unavailable=len(directories) - len(available_actions),
                             incomplete=bool(errors or any(c["reason"] for c in cases))))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold-dir", type=Path, default=ROOT / "tests/fixtures/gold")
    parser.add_argument("--private-root", type=Path, default=ROOT.parent / ".release-private/2026-10-29")
    parser.add_argument("--site", action="append", help="Select a site directory; may be repeated")
    args = parser.parse_args(argv)
    result = evaluate_all(args.gold_dir, args.private_root, args.site)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
