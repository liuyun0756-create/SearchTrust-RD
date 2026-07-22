import copy
import unittest

from app.report_v21.copy_contract import ReportCopyInvalid
from app.report_v21.evidence_ledger import build_evidence_ledger
from app.report_v21.gbp_rule_evaluator import evaluate_gbp_rules
from app.report_v21.normalize import normalize_report_copy_to_v21
from app.report_v21.review_corpus import build_review_corpus
from app.report_v21.rule_contract import ACTIVE_RULE_IDS
from app.report_v21.scoring import REQUIRED_LAYER_KEYS


def _action(layer_key="entity_presence"):
    return {
        "priority": "high",
        "task_title": "Add missing business identity details",
        "affected_layer": layer_key,
        "where_to_add": ["Page footer"],
        "what_to_add": ["Verified business details"],
        "example_copy": [],
        "implementation_notes": ["Verify details before publishing."],
        "completion_signals": ["The checked page shows the verified details."],
        "expected_effect": "Makes the business easier to verify.",
        "effort_level": "small",
    }


def _report_copy():
    return {
        "page_level": {
            "current_assessment": "The page has a usable service foundation but incomplete identity detail.",
            "existing_foundation": "The service intent and contact path are clear.",
            "main_limitation": "Several business identity details are missing.",
            "likely_search_outcome": "Visibility may remain less stable than better verified pages.",
            "competitive_interpretation": "Competitors with complete identity signals have an advantage.",
        },
        "layers": [
            {
                "layer_key": key,
                "summary": f"Assessment summary for {key}.",
                "explanation": f"Assessment explanation for {key}.",
                "suggested_fixes": ["Keep verified signals complete and consistent."],
                "action_items": [_action(key)] if key == "entity_presence" else [],
            }
            for key in REQUIRED_LAYER_KEYS
        ],
        "key_issues": [
            {
                "issue_title": "Business identity details are incomplete",
                "affected_layer": "entity_presence",
                "judgement": "The checked page is missing several verifiable business details.",
                "explanation": "Missing identity fields make the business harder to verify from the page alone.",
                "why_it_matters": "Users and search systems need stable real-world business signals.",
                "impacts": ["The page can look less complete than competing local pages."],
                "suggestions": ["Add the verified identity fields to visible page templates."],
                "recommended_actions": [_action()],
            }
        ],
        "optimization_path": {
            "must_execute_now": [_action()],
            "defer_until_later": [],
            "do_not_prioritize_yet": [],
            "roadmap": [],
            "fix_order_warning": "Verify the business identity before expanding promotional copy.",
            "completion_signals": ["Visible identity fields are complete and verified."],
        },
        "client_summary": {
            "title": "Complete the business identity first",
            "plain_language_summary": "The page explains the service but omits important business details.",
            "why_it_matters": "Incomplete identity signals make the page harder to trust.",
            "first_priority": "Add verified business identity details.",
            "not_first_priority": "Do not begin with cosmetic keyword edits.",
            "expected_change": "The page should become easier to verify and defend.",
        },
    }


def _context():
    content = "Spot On Plumbing\nEmergency plumbing services are available every day.\nCall (918) 818-3901"
    return {
        "task_id": "copy-contract-test",
        "url": "https://example.com/emergency-services/",
        "page_type": "Service Page",
        "generated_at": "2026-07-16T00:00:00+00:00",
        "content": content,
        "page_content": content,
        "content_checked": True,
        "business": {"name": "Spot On Plumbing", "phone": "(918) 818-3901"},
        "page_facts": {
            "business_names": ["Spot On Plumbing"],
            "phones": ["(918) 818-3901"],
            "addresses": [],
            "service_areas": [],
            "hours": [],
        },
        "gbp_data": {},
        "gbp_lookup_attempted": False,
        "sub_pages": [],
    }


class ReportCopyContractTests(unittest.TestCase):
    def _normalize(self, copy_payload=None):
        context = _context()
        ledger = build_evidence_ledger(context)
        results = {rule_id: rule_id in {21, 22, 23, 24, 25} for rule_id in ACTIVE_RULE_IDS}
        applicability = {rule_id: rule_id not in {26, 27, 28, 29} for rule_id in ACTIVE_RULE_IDS}
        outputs = {"report_copy_v2_1": copy_payload or _report_copy()}
        return normalize_report_copy_to_v21(
            outputs,
            context,
            results,
            applicability,
            ledger,
        )["report_v2_1"]

    def test_backend_builds_rules_scores_coverage_and_evidence(self):
        report = self._normalize()
        entity_layer = next(layer for layer in report["layers"] if layer["layer_key"] == "entity_presence")

        self.assertEqual(entity_layer["triggered_rule_ids"], [21, 22, 23, 24, 25])
        self.assertEqual(entity_layer["status"], "weak")
        self.assertEqual(len(entity_layer["evidence_items"]), 5)
        self.assertTrue(report["data_coverage"]["page_content_checked"])
        self.assertEqual(report["gbp_status"]["status"], "not_checked")
        self.assertNotEqual(report["overall_status"]["explanation"], "Pending deterministic scoring.")

    def test_same_vector_produces_identical_objective_results_three_times(self):
        reports = [self._normalize() for _ in range(3)]
        projections = [
            {
                "layers": [(layer["layer_key"], layer["status"], layer["triggered_rule_ids"]) for layer in report["layers"]],
                "overall": report["overall_status"],
                "ranking": report["ranking_potential"],
                "risk": report["risk_level"],
                "page_level": report["page_level"]["label"],
            }
            for report in reports
        ]
        self.assertEqual(projections[0], projections[1])
        self.assertEqual(projections[1], projections[2])

    def test_chinese_copy_is_rejected(self):
        payload = copy.deepcopy(_report_copy())
        payload["client_summary"]["title"] = "中文报告"
        with self.assertRaises(ReportCopyInvalid) as raised:
            self._normalize(payload)
        self.assertEqual(raised.exception.error_code, "V21_LANGUAGE_INVALID")

    def test_backend_builds_positive_rule_evidence_without_dify_references(self):
        context = _context()
        ledger = build_evidence_ledger(context)
        results = {rule_id: rule_id == 1 for rule_id in ACTIVE_RULE_IDS}
        applicability = {rule_id: True for rule_id in ACTIVE_RULE_IDS}
        report = normalize_report_copy_to_v21(
            {"report_copy_v2_1": _report_copy()},
            context,
            results,
            applicability,
            ledger,
        )["report_v2_1"]
        layer = next(item for item in report["layers"] if item["layer_key"] == "specificity")
        self.assertEqual(layer["triggered_rule_ids"], [1])
        self.assertTrue(layer["evidence_items"])
        self.assertEqual(layer["evidence_items"][0]["source_type"], "page")

    def test_review_rules_use_the_same_backend_review_corpus(self):
        context = _context()
        context["gbp_data"] = {
            "review_list": [{"text": "They repaired our water heater in Tulsa."}],
        }
        context["review_corpus"] = build_review_corpus(
            context["content"],
            context["gbp_data"],
            page_url=context["url"],
        )
        ledger = build_evidence_ledger(context)
        results = {rule_id: rule_id in {37, 38} for rule_id in ACTIVE_RULE_IDS}
        applicability = {rule_id: True for rule_id in ACTIVE_RULE_IDS}
        report = normalize_report_copy_to_v21(
            {"report_copy_v2_1": _report_copy()},
            context,
            results,
            applicability,
            ledger,
        )["report_v2_1"]
        layer = next(item for item in report["layers"] if item["layer_key"] == "algorithm_fit")
        self.assertEqual(layer["triggered_rule_ids"], [37, 38])
        self.assertEqual(len(layer["evidence_items"]), 1)
        self.assertEqual(layer["evidence_items"][0]["source_type"], "review")

    def test_structured_gbp_values_use_json_text(self):
        context = _context()
        context["gbp_data"] = {"hours": {"monday": "Open 24 hours"}}
        ledger = build_evidence_ledger(context)
        self.assertEqual(
            ledger["gbp-hours-01"]["extracted_text"],
            '{"monday": "Open 24 hours"}',
        )

    def test_gbp_key_issues_bind_one_rule_action_and_evidence_each(self):
        context = _context()
        context["content"] += "\n1911 West Reno Street, Broken Arrow, OK 74012"
        context["page_content"] = context["content"]
        context["page_business"] = {"name": "Spot On Plumbing", "phone": "(918) 818-3901"}
        context.update({
            "input_gbp_url": "https://maps.google.com/example",
            "gbp_url": "https://maps.google.com/example",
            "gbp_lookup_attempted": True,
            "page_facts": {
                "business_names": ["Spot On Plumbing"],
                "addresses": ["1911 West Reno Street, Broken Arrow, OK 74012"],
                "phones": ["(918) 818-3901"],
                "service_areas": ["Tulsa"],
            },
            "gbp_data": {
                "name": "Spot On Plumbing",
                "address": "1911 W Reno St, Broken Arrow, OK 74012",
                "phone": "(918) 844-7961",
                "service_areas": ["Tulsa"],
            },
        })
        results = {rule_id: False for rule_id in ACTIVE_RULE_IDS}
        applicability = {rule_id: True for rule_id in ACTIVE_RULE_IDS}
        backend_results, backend_applicability, findings = evaluate_gbp_rules(context)
        results.update(backend_results)
        applicability.update(backend_applicability)
        context["backend_gbp_findings"] = findings
        payload = copy.deepcopy(_report_copy())
        payload["key_issues"] = []
        for rule_id, title in ((27, "Address identity differs"), (28, "Phone identity differs")):
            action = _action("entity_consistency")
            payload["key_issues"].append({
                "finding_key": f"rule_{rule_id}",
                "issue_title": title,
                "affected_layer": "entity_consistency",
                "judgement": title,
                "explanation": "The checked values are not exactly aligned.",
                "why_it_matters": "Stable identity fields help users verify the business.",
                "impacts": ["The page and profile can present conflicting identity signals."],
                "suggestions": ["Confirm and publish one canonical value."],
                "recommended_actions": [action],
            })

        report = normalize_report_copy_to_v21(
            {"report_copy_v2_1": payload},
            context,
            results,
            applicability,
            build_evidence_ledger(context),
        )["report_v2_1"]

        issues = [item for item in report["key_issues"] if item["affected_layer"] == "entity_consistency"]
        self.assertEqual([item["related_rule_ids"] for item in issues], [[27], [28]])
        self.assertEqual([item["recommended_actions"][0]["related_rule_ids"] for item in issues], [[27], [28]])
        self.assertTrue(all(all(f"rule-{rule_id}" in evidence["id"] or evidence["id"] == f"bp-{key}"
                                for evidence in issue["evidence_items"])
                            for issue, rule_id, key in zip(issues, (27, 28), ("address", "phone"))))

    def test_medium_entity_consistency_requires_each_triggered_finding_key(self):
        context = _context()
        ledger = build_evidence_ledger(context)
        results = {rule_id: rule_id in {27, 28} for rule_id in ACTIVE_RULE_IDS}
        applicability = {rule_id: True for rule_id in ACTIVE_RULE_IDS}
        payload = copy.deepcopy(_report_copy())
        payload["key_issues"] = [{
            "finding_key": "rule_27",
            "issue_title": "Address identity differs",
            "affected_layer": "entity_consistency",
            "judgement": "The address differs.",
            "explanation": "The values are not aligned.",
            "why_it_matters": "Identity should be stable.",
            "impacts": ["Conflicting identity signal."],
            "suggestions": ["Confirm one address."],
            "recommended_actions": [_action("entity_consistency")],
        }]

        with self.assertRaises(ReportCopyInvalid) as raised:
            normalize_report_copy_to_v21(
                {"report_copy_v2_1": payload},
                context,
                results,
                applicability,
                ledger,
            )
        self.assertIn("rule_28", " ".join(raised.exception.details))


if __name__ == "__main__":
    unittest.main()
