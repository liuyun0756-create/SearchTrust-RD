import copy
import json
import unittest
from pathlib import Path

from app.report_v21.normalize import (
    ReportV21OutputInvalid,
    normalize_native_report_to_v21,
)
from app.report_v21.dedupe import dedupe_report_v21


FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "report_v21"
    / "fixtures"
    / "real_dify_array_output.json"
)


def all_action_items(value):
    if isinstance(value, list):
        for item in value:
            yield from all_action_items(item)
    elif isinstance(value, dict):
        if "task_title" in value and "affected_layer" in value:
            yield value
        for item in value.values():
            yield from all_action_items(item)


class ReportV21ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real_dify_output = json.loads(FIXTURE_PATH.read_text())

    def normalize(self, payload, **context_overrides):
        context = {
            "task_id": "contract-fixture",
            "url": "https://spotonplumbing.com/emergency-services/",
            "page_type": "本地服务落地页",
            "generated_at": "2026-07-09T17:41:20+00:00",
            "input_gbp_url": "",
            "gbp_url": "",
            "gbp_data": {},
            "content_checked": True,
            "scraper_source": "fixture",
            "sub_pages": [],
            "raw_content_length": 1,
            "gbp_lookup_attempted": False,
            "gbp_error": None,
        }
        context.update(context_overrides)
        return normalize_native_report_to_v21(copy.deepcopy(payload), context)["report_v2_1"]

    def test_real_dify_array_output_contains_string_array_examples(self):
        actions = list(all_action_items(self.real_dify_output))

        self.assertGreaterEqual(len(actions), 12)
        self.assertTrue(all(isinstance(action["example_copy"], list) for action in actions))
        self.assertTrue(
            all(
                all(isinstance(example, str) for example in action["example_copy"])
                for action in actions
            )
        )

    def test_contract_compliant_projection_normalizes_without_losing_examples(self):
        payload = self.contract_compliant_projection(self.real_dify_output)
        report = self.normalize(payload)
        actions = list(all_action_items(report))

        self.assertGreaterEqual(len(actions), 12)
        self.assertTrue(all(isinstance(action["example_copy"], list) for action in actions))
        self.assertTrue(
            all(
                all(isinstance(example, str) for example in action["example_copy"])
                for action in actions
            )
        )
        self.assertEqual(report["gbp_status"]["status"], "not_checked")

    def test_legacy_scalar_example_copy_is_wrapped_once(self):
        payload = self.contract_compliant_projection(self.real_dify_output)
        payload["report_v2_1"]["layers"][0]["action_items"][0]["example_copy"] = "Keep the emergency CTA visible."

        report = self.normalize(payload)
        example_copy = report["layers"][0]["action_items"][0]["example_copy"]

        self.assertEqual(example_copy, ["Keep the emergency CTA visible."])

    def test_non_string_example_copy_items_are_rejected(self):
        payload = self.contract_compliant_projection(self.real_dify_output)
        payload["report_v2_1"]["layers"][0]["action_items"][0]["example_copy"] = [{"copy": "invalid"}]

        with self.assertRaises(ReportV21OutputInvalid):
            self.normalize(payload)

    def test_backend_owns_all_gbp_states(self):
        cases = {
            "not_checked": ({}, "not_available"),
            "checked": ({
                "input_gbp_url": "https://maps.google.com/?cid=fixture",
                "gbp_data": {"name": "Spot On Plumbing"},
            }, "user_provided"),
            "not_found": ({
                "input_gbp_url": "https://www.google.com/maps?cid=fixture",
                "gbp_lookup_attempted": True,
            }, "user_provided"),
            "error": ({"gbp_error": "fixture timeout"}, "not_available"),
        }

        for expected_status, (overrides, expected_source) in cases.items():
            with self.subTest(expected_status=expected_status):
                report = self.normalize(
                    self.contract_compliant_projection(self.real_dify_output),
                    **overrides,
                )
                self.assertEqual(report["gbp_status"]["status"], expected_status)
                self.assertEqual(report["gbp_status"]["source"], expected_source)
                self.assertEqual(
                    report["data_coverage"]["gbp_checked"],
                    expected_status == "checked",
                )

    def test_system_discovered_gbp_is_checked_without_user_input(self):
        report = self.normalize(
            self.contract_compliant_projection(self.real_dify_output),
            gbp_url="https://maps.google.com/?cid=discovered",
            gbp_data={"name": "Spot On Plumbing"},
            gbp_lookup_attempted=True,
        )

        self.assertEqual(report["gbp_status"]["status"], "checked")
        self.assertEqual(report["gbp_status"]["source"], "system_discovered")
        self.assertTrue(report["data_coverage"]["gbp_checked"])

    def test_gbp_not_found_uses_backend_lookup_diagnostic(self):
        report = self.normalize(
            self.contract_compliant_projection(self.real_dify_output),
            input_gbp_url="https://maps.app.goo.gl/fixture",
            gbp_url="https://maps.app.goo.gl/fixture",
            gbp_lookup_attempted=True,
            gbp_lookup_diagnostic={
                "status": "not_found",
                "code": "strict_fallback_no_match",
                "message": "The exact CID and strict website-domain fallback returned no match.",
            },
        )

        self.assertEqual(report["gbp_status"]["status"], "not_found")
        self.assertEqual(
            report["gbp_status"]["reason"],
            "The exact CID and strict website-domain fallback returned no match.",
        )

    def test_unchecked_gbp_removes_alignment_assessment_language(self):
        payload = self.contract_compliant_projection(self.real_dify_output)
        payload["report_v2_1"]["primary_blocking_layer"]["reason"] = (
            "The page contact details do not clearly align with the supplied GBP data, "
            "so a reliable entity match assessment could not be completed."
        )

        report = self.normalize(payload)
        reason = report["primary_blocking_layer"]["reason"]

        self.assertEqual(
            reason,
            "GBP was not checked in this report, so GBP alignment could not be verified.",
        )

    def test_checked_gbp_overrides_unavailable_dify_narrative(self):
        payload = self.contract_compliant_projection(self.real_dify_output)
        report = payload["report_v2_1"]
        report["primary_blocking_layer"]["reason"] = (
            "The provided GBP data could not be reliably checked, so cross-platform "
            "identity alignment could not be verified."
        )
        evidence = report["primary_blocking_layer"]["evidence_items"][0]
        evidence["source_type"] = "not_available"
        evidence["source_label"] = "GBP lookup error"
        evidence["explanation"] = "The supplied GBP check failed."

        normalized = self.normalize(payload, gbp_data={"name": "Spot On Plumbing"})
        serialized = json.dumps(normalized)

        self.assertEqual(normalized["gbp_status"]["status"], "checked")
        self.assertEqual(
            normalized["primary_blocking_layer"]["reason"],
            "Backend-verified GBP data was available for this report. Specific alignment "
            "claims are limited to the evidence shown.",
        )
        self.assertEqual(
            normalized["primary_blocking_layer"]["evidence_items"][0]["source_type"],
            "gbp",
        )
        self.assertNotIn("GBP lookup error", serialized)
        self.assertNotIn("could not be reliably checked", serialized)

    def test_dedupe_preserves_example_copy_arrays(self):
        report = self.normalize(self.contract_compliant_projection(self.real_dify_output))
        action = copy.deepcopy(report["layers"][0]["action_items"][0])
        duplicate = copy.deepcopy(action)
        duplicate["example_copy"] = ["A second evidence-supported example."]
        report["layers"][0]["action_items"] = [action, duplicate]

        deduped, warnings = dedupe_report_v21(report)

        merged = deduped["layers"][0]["action_items"]
        self.assertEqual(len(merged), 1)
        self.assertEqual(
            merged[0]["example_copy"],
            [*action["example_copy"], "A second evidence-supported example."],
        )
        self.assertTrue(warnings)

    def test_backend_overrides_layer_labels_and_assessment_coverage(self):
        payload = self.contract_compliant_projection(self.real_dify_output)
        layers = payload["report_v2_1"]["layers"]
        specificity = next(layer for layer in layers if layer["layer_key"] == "specificity")
        specificity["layer_name"] = "Dify supplied label"
        specificity["layer_label"] = "L1 wrong numbering"
        specificity["checked_rule_ids"] = [1, 2, 4, 6, 7, 8, 32]

        report = self.normalize(payload)
        scored_specificity = next(layer for layer in report["layers"] if layer["layer_key"] == "specificity")

        self.assertEqual(scored_specificity["layer_id"], 4)
        self.assertEqual(scored_specificity["layer_name"], "Specificity")
        self.assertEqual(scored_specificity["layer_label"], "L4 Specificity")
        self.assertEqual(scored_specificity["checked_rule_ids"], [1, 2, 4, 6, 7, 8, 32, 34, 35, 36])

    def test_backend_ranking_potential_uses_confirmed_priority(self):
        payload = self.contract_compliant_projection(self.real_dify_output)
        rules_by_layer = {
            "foundation": [17, 18, 19, 20],
            "entity_presence": [21, 22, 23, 24, 25],
            "entity_consistency": [],
            "specificity": [],
            "real_world_connection": [],
            "accountability": [],
            "page_unique_value": [13, 14, 16],
            "algorithm_fit": [15, 37, 38, 39],
        }
        for layer in payload["report_v2_1"]["layers"]:
            layer["triggered_rule_ids"] = rules_by_layer[layer["layer_key"]]

        report = self.normalize(payload)

        self.assertEqual(report["ranking_potential"]["level"], "low")
        self.assertEqual(report["ranking_potential"]["label"], "Low Potential")

    def test_backend_uses_product_labels_and_customer_facing_explanations(self):
        report = self.normalize(self.contract_compliant_projection(self.real_dify_output))

        self.assertIn(report["overall_status"]["label"], {"Weak", "Medium Weak", "Medium", "Good"})
        self.assertIn(
            report["ranking_potential"]["label"],
            {"Strong Competitive Potential", "Improvement Potential", "Competitive", "Low Potential"},
        )
        for field in ("overall_status", "ranking_potential", "risk_level"):
            self.assertNotIn("deterministic scoring", report[field]["explanation"].lower())

    def test_backend_adds_readable_findings_without_exposing_rule_numbers(self):
        report = self.normalize(self.contract_compliant_projection(self.real_dify_output))

        for layer in report["layers"]:
            self.assertEqual(len(layer["triggered_findings"]), len(layer["triggered_rule_ids"]))
            self.assertTrue(all("rule_" not in finding.lower() for finding in layer["triggered_findings"]))

    def test_page_level_preserves_richer_dify_narrative(self):
        payload = self.contract_compliant_projection(self.real_dify_output)
        page_level = payload["report_v2_1"]["page_level"]
        page_level.update({
            "current_assessment": "Page-specific current assessment.",
            "existing_foundation": "Page-specific existing foundation.",
            "main_limitation": "Page-specific main limitation.",
            "likely_search_outcome": "Page-specific likely outcome.",
            "competitive_interpretation": "Page-specific competitive interpretation.",
        })

        report = self.normalize(payload)

        self.assertEqual(report["page_level"]["current_assessment"], "Page-specific current assessment.")
        self.assertEqual(report["page_level"]["existing_foundation"], "Page-specific existing foundation.")
        self.assertEqual(report["page_level"]["main_limitation"], "Page-specific main limitation.")
        self.assertEqual(report["page_level"]["likely_search_outcome"], "Page-specific likely outcome.")
        self.assertEqual(
            report["page_level"]["competitive_interpretation"],
            "Page-specific competitive interpretation.",
        )

    def test_key_issue_presentation_fields_have_safe_legacy_fallbacks(self):
        report = self.normalize(self.contract_compliant_projection(self.real_dify_output))
        issue = report["key_issues"][0]

        self.assertTrue(issue["judgement"])
        self.assertTrue(issue["impacts"])
        self.assertTrue(issue["suggestions"])

    def test_dedupe_merges_same_excerpt_across_url_and_section_variants(self):
        report = self.normalize(self.contract_compliant_projection(self.real_dify_output))
        specificity = next(layer for layer in report["layers"] if layer["layer_key"] == "specificity")
        specificity["evidence_items"] = [
            {
                "id": "ev-variant-1",
                "source_type": "page",
                "source_label": "Body copy",
                "source_url": "https://example.com/service/",
                "page_section": "Main content",
                "extracted_text": "24-hour plumbing services, seven days a week",
                "comparison_result": "partial",
                "confidence": "medium",
                "explanation": "The wording is generic.",
            },
            {
                "id": "ev-variant-2",
                "source_type": "page",
                "source_label": "Service copy",
                "source_url": "https://example.com/service?ref=audit",
                "page_section": "Body copy",
                "extracted_text": "24-hour plumbing services, seven days a week.",
                "comparison_result": "partial",
                "confidence": "medium",
                "explanation": "The wording lacks a local scenario.",
            },
        ]

        deduped, _ = dedupe_report_v21(report)
        deduped_specificity = next(layer for layer in deduped["layers"] if layer["layer_key"] == "specificity")

        self.assertEqual(len(deduped_specificity["evidence_items"]), 1)

    def test_native_conclusion_without_dify_evidence_is_not_rejected(self):
        payload = self.contract_compliant_projection(self.real_dify_output)
        report = payload["report_v2_1"]
        issue = next(issue for issue in report["key_issues"] if issue["severity"] in {"high", "medium"})
        issue["evidence_items"] = []
        issue["recommended_actions"] = []
        layer = next(layer for layer in report["layers"] if layer["layer_key"] == issue["affected_layer"])
        layer["evidence_items"] = []

        normalized = self.normalize(payload)
        self.assertEqual(normalized["schema_version"], "2.1")

    def test_backend_adds_verified_gbp_profile_and_conservative_alignment_rows(self):
        report = self.normalize(
            self.contract_compliant_projection(self.real_dify_output),
            input_gbp_url="https://maps.google.com/?cid=fixture",
            gbp_url="https://maps.google.com/?cid=fixture",
            business={"name": "Spot On Plumbing", "phone": "(918) 818-3901"},
            gbp_data={
                "name": "Spot On Plumbing",
                "phone": "918 818 3901",
                "address": "1911 W Reno St, Broken Arrow, OK 74012",
                "website": "https://spotonplumbing.com/",
                "type": "Plumber",
                "hours": "Open 24 hours",
                "rating": "4.9",
                "reviews": "2180",
                "service_areas": ["Tulsa", "Broken Arrow"],
            },
        )

        self.assertEqual(report["gbp_profile"]["name"], "Spot On Plumbing")
        self.assertEqual(report["gbp_profile"]["categories"], ["Plumber"])
        rows = {row["field_key"]: row for row in report["gbp_alignment"]}
        self.assertEqual(rows["phone"]["status"], "match")
        self.assertEqual(rows["address"]["status"], "not_checked")

    def test_backend_exposes_only_checked_schema_summary(self):
        report = self.normalize(
            self.contract_compliant_projection(self.real_dify_output),
            schema_data={"checked": True, "source_url": "https://spotonplumbing.com/emergency-services/", "types": ["LocalBusiness", "Service"]},
        )

        self.assertTrue(report["data_coverage"]["schema_checked"])
        self.assertEqual(report["schema_summary"]["types"], ["LocalBusiness", "Service"])

    def contract_compliant_projection(self, payload):
        """Model the shape Dify must emit after replacing legacy string actions."""
        projected = copy.deepcopy(payload)
        report = projected["report_v2_1"]
        actions = list(all_action_items(report))

        def find_action(term):
            for action in actions:
                if term.lower() in action["task_title"].lower():
                    return copy.deepcopy(action)
            self.fail(f"fixture action not found: {term}")

        report["optimization_path"]["must_execute_now"] = [
            find_action("standardize public contact"),
            find_action("real service case"),
        ]
        report["optimization_path"]["defer_until_later"] = [
            find_action("grounded geography"),
            find_action("realistic service limitations"),
        ]
        report["optimization_path"]["do_not_prioritize_yet"] = []
        report["optimization_path"]["roadmap"][0]["action_items"] = [
            find_action("standardize public contact"),
        ]
        report["optimization_path"]["roadmap"][1]["action_items"] = [
            find_action("real service case"),
            find_action("realistic service limitations"),
        ]
        report["optimization_path"]["roadmap"][2]["action_items"] = [
            find_action("grounded geography"),
        ]
        return projected
