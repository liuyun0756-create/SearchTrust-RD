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
            "not_checked": {},
            "checked": {"gbp_data": {"name": "Spot On Plumbing"}},
            "not_found": {
                "input_gbp_url": "https://www.google.com/maps?cid=fixture",
                "gbp_lookup_attempted": True,
            },
            "error": {"gbp_error": "fixture timeout"},
        }

        for expected_status, overrides in cases.items():
            with self.subTest(expected_status=expected_status):
                report = self.normalize(
                    self.contract_compliant_projection(self.real_dify_output),
                    **overrides,
                )
                self.assertEqual(report["gbp_status"]["status"], expected_status)
                self.assertEqual(
                    report["data_coverage"]["gbp_checked"],
                    expected_status == "checked",
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
