import copy
import unittest

from app.report_v21.copy_contract import (
    ReportCopyInvalid,
    ReportCopyV21,
    assemble_report_skeleton,
)
from app.report_v21.action_requirements import (
    ACTION_REQUIREMENTS_BY_KEY,
    active_action_requirements,
)
from app.report_v21.evidence_ledger import build_evidence_ledger
from app.report_v21.gbp_rule_evaluator import evaluate_gbp_rules
from app.report_v21.normalize import normalize_report_copy_to_v21
from app.report_v21.review_corpus import build_review_corpus
from app.report_v21.rule_contract import ACTIVE_RULE_IDS
from app.report_v21.scoring import REQUIRED_LAYER_KEYS, build_client_decision_context


def _action(requirement, triggered_ids):
    return {
        "action_key": requirement.action_key,
        "covers_finding_keys": [f"rule_{rule_id}" for rule_id in triggered_ids],
        "priority": requirement.priority,
        "task_title": f"Complete {requirement.action_key.replace('_', ' ')}",
        "affected_layer": requirement.affected_layer,
        "where_to_add": ["Page footer"],
        "what_to_add": ["Verified business details"],
        "example_copy": [],
        "implementation_notes": ["Verify details before publishing."],
        "completion_signals": ["The checked page shows the verified details."],
        "expected_effect": "Makes the business easier to verify.",
        "effort_level": requirement.effort_level,
    }


def _report_copy(triggered_ids=None):
    triggered_ids = set(triggered_ids or {21, 22, 23, 24, 25})
    results = {rule_id: rule_id in triggered_ids for rule_id in ACTIVE_RULE_IDS}
    applicability = {rule_id: True for rule_id in ACTIVE_RULE_IDS}
    active = active_action_requirements(results, applicability)
    catalog = [
        _action(requirement, active_rule_ids)
        for requirement, active_rule_ids in active.values()
    ]
    key_issues = [
        {
            "finding_keys": [f"rule_{rule_id}" for rule_id in active_rule_ids],
            "issue_title": f"Work is required for {requirement.action_key}",
            "affected_layer": requirement.affected_layer,
            "judgement": "The checked page triggered this remediation group.",
            "explanation": "The corresponding findings require a concrete implementation task.",
            "why_it_matters": "Completing the task will repair the affected trust signals.",
            "impacts": ["The affected trust layer remains incomplete."],
            "suggestions": ["Complete the specified remediation task."],
            "recommended_action_keys": [requirement.action_key],
        }
        for requirement, active_rule_ids in active.values()
    ]
    action_keys = [item["action_key"] for item in catalog]
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
            }
            for key in REQUIRED_LAYER_KEYS
        ],
        "action_catalog": catalog,
        "key_issues": key_issues,
        "optimization_path": {
            "must_execute_now_action_keys": action_keys,
            "defer_until_later_action_keys": [],
            "do_not_prioritize_yet_action_keys": [],
            "roadmap": [{
                "phase_title": "Complete the active remediation work",
                "sequence": 1,
                "goal": "Resolve the confirmed findings.",
                "entry_condition": "The findings have been confirmed.",
                "action_keys": action_keys,
                "expected_outcomes": ["The confirmed findings are addressed."],
            }],
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

    def test_layer_actions_cover_every_triggered_finding_without_one_action_per_rule(self):
        triggered_ids = {2, 4, 6, 7, 8, 32, 34}
        context = _context()
        report = normalize_report_copy_to_v21(
            {"report_copy_v2_1": _report_copy(triggered_ids)},
            context,
            {rule_id: rule_id in triggered_ids for rule_id in ACTIVE_RULE_IDS},
            {rule_id: True for rule_id in ACTIVE_RULE_IDS},
            build_evidence_ledger(context),
        )["report_v2_1"]

        layer = next(item for item in report["layers"] if item["layer_key"] == "specificity")
        self.assertEqual(layer["triggered_rule_ids"], [2, 4, 6, 7, 8, 32, 34])
        self.assertEqual(len(layer["action_items"]), 4)
        self.assertEqual(
            {
                rule_id
                for action in layer["action_items"]
                for rule_id in action["related_rule_ids"]
            },
            triggered_ids,
        )
        layer_action_ids = {action["id"] for action in layer["action_items"]}
        issue_action_ids = {
            action["id"]
            for issue in report["key_issues"]
            for action in issue["recommended_actions"]
        }
        self.assertEqual(layer_action_ids, issue_action_ids)
        self.assertEqual(
            layer_action_ids,
            {action["id"] for action in report["optimization_path"]["must_execute_now"]},
        )

    def test_missing_catalog_action_is_retryable(self):
        triggered_ids = {2, 4, 6, 7, 8, 32, 34}
        payload = _report_copy(triggered_ids)
        removed = payload["action_catalog"].pop()
        with self.assertRaises(ReportCopyInvalid) as raised:
            normalize_report_copy_to_v21(
                {"report_copy_v2_1": payload},
                _context(),
                {rule_id: rule_id in triggered_ids for rule_id in ACTIVE_RULE_IDS},
                {rule_id: True for rule_id in ACTIVE_RULE_IDS},
                build_evidence_ledger(_context()),
            )
        self.assertIn(removed["action_key"], " ".join(raised.exception.details))

    def test_known_inactive_actions_and_references_are_discarded(self):
        triggered_ids = {2, 7}
        payload = copy.deepcopy(_report_copy(triggered_ids))
        inactive_requirement = ACTION_REQUIREMENTS_BY_KEY["foundation_entity_scope"]
        inactive_action = _action(inactive_requirement, (17,))
        payload["action_catalog"].append(inactive_action)
        payload["key_issues"].append({
            "finding_keys": ["rule_17"],
            "issue_title": "Inactive draft issue",
            "affected_layer": "foundation",
            "judgement": "This draft must not enter the final report.",
            "explanation": "The authoritative rule vector did not activate it.",
            "why_it_matters": "It is an inactive Dify draft.",
            "impacts": ["None in the final report."],
            "suggestions": ["Discard this draft."],
            "recommended_action_keys": [inactive_requirement.action_key],
        })
        payload["optimization_path"]["must_execute_now_action_keys"].append(
            inactive_requirement.action_key
        )
        payload["optimization_path"]["roadmap"].append({
            "phase_title": "Inactive phase",
            "sequence": 2,
            "goal": "This phase must be discarded.",
            "entry_condition": "The inactive rule would need to trigger.",
            "action_keys": [inactive_requirement.action_key],
            "expected_outcomes": ["No inactive work appears."],
        })

        report = normalize_report_copy_to_v21(
            {"report_copy_v2_1": payload},
            _context(),
            {rule_id: rule_id in triggered_ids for rule_id in ACTIVE_RULE_IDS},
            {rule_id: True for rule_id in ACTIVE_RULE_IDS},
            build_evidence_ledger(_context()),
        )["report_v2_1"]

        all_action_ids = {
            action["id"]
            for layer in report["layers"]
            for action in layer["action_items"]
        }
        self.assertNotIn(
            f"act-{inactive_requirement.action_key}",
            all_action_ids,
        )
        self.assertNotIn(
            f"issue-{inactive_requirement.action_key}",
            {issue["id"] for issue in report["key_issues"]},
        )
        self.assertNotIn(
            inactive_requirement.action_key,
            {
                action["id"].removeprefix("act-")
                for phase in report["optimization_path"]["roadmap"]
                for action in phase["action_items"]
            },
        )

    def test_unknown_action_key_remains_retryable(self):
        payload = copy.deepcopy(_report_copy({2, 7}))
        unknown_action = copy.deepcopy(payload["action_catalog"][0])
        unknown_action["action_key"] = "unknown_action_group"
        payload["action_catalog"].append(unknown_action)

        with self.assertRaises(ReportCopyInvalid) as raised:
            normalize_report_copy_to_v21(
                {"report_copy_v2_1": payload},
                _context(),
                {rule_id: rule_id in {2, 7} for rule_id in ACTIVE_RULE_IDS},
                {rule_id: True for rule_id in ACTIVE_RULE_IDS},
                build_evidence_ledger(_context()),
            )
        self.assertIn("unknown_action_group", " ".join(raised.exception.details))

    def test_client_decision_context_uses_final_key_issues_without_changing_scores(self):
        report = {
            "key_issues": [
                {"affected_layer": "entity_consistency"},
                {"affected_layer": "entity_consistency"},
                {"affected_layer": "specificity"},
                {"affected_layer": "accountability"},
            ],
            "overall_status": {"label": "Medium Weak", "level": "medium_weak"},
            "ranking_potential": {"label": "Strong Competitive Potential", "level": "strong"},
            "risk_level": {"label": "Low", "level": "low"},
        }
        objective_scores = copy.deepcopy({
            "overall_status": report["overall_status"],
            "ranking_potential": report["ranking_potential"],
            "risk_level": report["risk_level"],
        })

        context = build_client_decision_context(report)

        self.assertEqual(context["priority_level"], "immediate")
        self.assertEqual(context["issue_count"], 4)
        self.assertEqual(context["affected_layer_count"], 3)
        self.assertEqual(context["work_phase_count"], 3)
        self.assertEqual(
            [phase["stage"] for phase in context["work_sequence"]],
            ["fix_first", "build_next", "strengthen_after"],
        )
        self.assertEqual(
            context["work_sequence"][0]["layer_keys"],
            ["entity_consistency"],
        )
        self.assertEqual(
            objective_scores,
            {
                "overall_status": report["overall_status"],
                "ranking_potential": report["ranking_potential"],
                "risk_level": report["risk_level"],
            },
        )

    def test_client_decision_priority_mapping(self):
        def decision_for(layer_key=None):
            return build_client_decision_context({
                "key_issues": [] if layer_key is None else [{"affected_layer": layer_key}],
                "overall_status": {"label": "Good"},
                "ranking_potential": {"label": "Competitive"},
                "risk_level": {"label": "Low"},
            })

        self.assertEqual(decision_for("entity_consistency")["priority_level"], "immediate")
        self.assertEqual(decision_for("specificity")["priority_level"], "high")
        self.assertEqual(decision_for("accountability")["priority_level"], "planned")
        self.assertEqual(decision_for()["priority_level"], "monitor")
        self.assertEqual(decision_for()["work_phase_count"], 0)

    def test_primary_blocker_uses_earliest_triggered_layer_and_keeps_good_layer_issue(self):
        triggered_ids = {1, 2, 4, 6, 7, 8, 9, 27, 28, 32, 34}
        payload = copy.deepcopy(_report_copy(triggered_ids))
        results = {rule_id: rule_id in triggered_ids for rule_id in ACTIVE_RULE_IDS}
        applicability = {rule_id: True for rule_id in ACTIVE_RULE_IDS}

        report = assemble_report_skeleton(
            ReportCopyV21.model_validate(payload),
            results,
            applicability,
            {},
            _context(),
        )

        self.assertEqual(report["primary_blocking_layer"]["layer_key"], "entity_consistency")
        issue_by_layer = {item["affected_layer"]: item for item in report["key_issues"]}
        self.assertIn("accountability", issue_by_layer)
        self.assertEqual(issue_by_layer["accountability"]["severity"], "low")

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
            {"report_copy_v2_1": _report_copy({1})},
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
            {"report_copy_v2_1": _report_copy({37, 38})},
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
        payload = copy.deepcopy(_report_copy({27, 28}))

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
        payload = copy.deepcopy(_report_copy({27, 28}))
        payload["key_issues"] = [
            issue
            for issue in payload["key_issues"]
            if issue["finding_keys"] != ["rule_28"]
        ]

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
