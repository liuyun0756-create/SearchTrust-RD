import unittest

from app.report_v21.page_facts import build_page_facts
from app.report_v21.rule_contract import (
    ACTIVE_RULE_IDS,
    RetryableDifyOutputError,
    parse_rule_results,
    validate_english_narrative,
)


def _complete_vector(value=False):
    return {f"rule_{rule_id}": value for rule_id in ACTIVE_RULE_IDS}


class RuleContractTests(unittest.TestCase):
    def test_complete_vector_requires_every_active_rule_and_boolean_values(self):
        results = _complete_vector(False)
        applicability = _complete_vector(True)
        results["rule_26"] = True

        parsed_results, parsed_applicability = parse_rule_results({
            "rule_results": results,
            "rule_applicability": applicability,
        })

        self.assertTrue(parsed_results[26])
        self.assertTrue(parsed_applicability[26])
        self.assertEqual(len(parsed_results), 38)
        self.assertNotIn(5, parsed_results)

    def test_missing_or_non_boolean_rule_result_retries_the_workflow(self):
        results = _complete_vector(False)
        applicability = _complete_vector(True)
        results.pop("rule_39")
        results["rule_1"] = "false"

        with self.assertRaises(RetryableDifyOutputError) as raised:
            parse_rule_results({
                "rule_results": results,
                "rule_applicability": applicability,
            })

        self.assertTrue(raised.exception.retryable)

    def test_non_applicable_gbp_rule_cannot_trigger(self):
        results = _complete_vector(False)
        applicability = _complete_vector(True)
        results["rule_29"] = True
        applicability["rule_29"] = False

        with self.assertRaises(RetryableDifyOutputError):
            parse_rule_results({
                "rule_results": results,
                "rule_applicability": applicability,
            })

    def test_missing_gbp_snapshot_can_trigger_all_comparison_rules(self):
        results = _complete_vector(False)
        applicability = _complete_vector(True)
        for rule_id in (26, 27, 28, 29):
            results[f"rule_{rule_id}"] = True

        parsed_results, parsed_applicability = parse_rule_results({
            "rule_results": results,
            "rule_applicability": applicability,
            "rule_evidence_ids": "this legacy output is ignored",
        })

        self.assertTrue(all(parsed_results[rule_id] for rule_id in (26, 27, 28, 29)))
        self.assertTrue(all(parsed_applicability[rule_id] for rule_id in (26, 27, 28, 29)))

    def test_backend_overrides_gbp_rules_and_dify_may_omit_them(self):
        results = _complete_vector(False)
        applicability = _complete_vector(True)
        for rule_id in (26, 27, 28, 29):
            results.pop(f"rule_{rule_id}")
            applicability.pop(f"rule_{rule_id}")

        parsed_results, parsed_applicability = parse_rule_results(
            {
                "rule_results": results,
                "rule_applicability": applicability,
            },
            backend_gbp_results={26: False, 27: True, 28: True, 29: False},
            backend_gbp_applicability={26: True, 27: True, 28: True, 29: True},
        )

        self.assertFalse(parsed_results[26])
        self.assertTrue(parsed_results[27])
        self.assertTrue(parsed_results[28])
        self.assertFalse(parsed_results[29])
        self.assertTrue(all(parsed_applicability[rule_id] for rule_id in (26, 27, 28, 29)))

    def test_backend_ownership_ignores_only_dify_gbp_rule_errors(self):
        results = _complete_vector(False)
        applicability = _complete_vector(True)

        parsed_results, _ = parse_rule_results(
            {
                "rule_results": results,
                "rule_applicability": applicability,
                "rule_errors": [
                    "rule_26 deterministic output was incomplete",
                    "rule_29 deterministic output was incomplete",
                ],
            },
            backend_gbp_results={26: False, 27: True, 28: True, 29: False},
            backend_gbp_applicability={26: True, 27: True, 28: True, 29: True},
        )

        self.assertTrue(parsed_results[27])

        with self.assertRaises(RetryableDifyOutputError):
            parse_rule_results(
                {
                    "rule_results": results,
                    "rule_applicability": applicability,
                    "rule_errors": ["rule_1 returned invalid JSON"],
                },
                backend_gbp_results={26: False, 27: True, 28: True, 29: False},
                backend_gbp_applicability={26: True, 27: True, 28: True, 29: True},
            )

    def test_chinese_narrative_is_rejected_but_raw_evidence_is_exempt(self):
        outputs = {
            "report_v2_1": {
                "overall_status": {"label": "Good", "level": "strong", "explanation": "English"},
                "ranking_potential": {},
                "risk_level": {},
                "primary_blocking_layer": {},
                "page_level": {},
                "layers": [{"summary": "English", "evidence_items": [{"extracted_text": "中文原文"}]}],
                "key_issues": [],
                "optimization_path": {},
                "client_summary": {},
            },
        }
        validate_english_narrative(outputs)
        outputs["report_v2_1"]["layers"][0]["summary"] = "中文结论"

        with self.assertRaises(RetryableDifyOutputError):
            validate_english_narrative(outputs)

    def test_page_facts_keep_exact_identity_values_and_service_area_list(self):
        content = """
        Spot On Plumbing
        Call (918) 818-3901
        1911 W Reno St, Broken Arrow, OK 74012
        Serving Tulsa, Broken Arrow, Catoosa, or Sapulpa.
        Open 24 hours
        """
        facts = build_page_facts(content, {"name": "Spot On Plumbing"})

        self.assertEqual(facts["business_names"], ["Spot On Plumbing"])
        self.assertIn("(918) 818-3901", facts["phones"])
        self.assertEqual(facts["service_areas"], ["Tulsa", "Broken Arrow", "Catoosa", "Sapulpa"])
        self.assertTrue(facts["addresses"])
        self.assertEqual(facts["hours"], ["Open 24 hours"])

    def test_page_facts_ignore_service_area_links_and_footer_copyright(self):
        content = """
        [Service Areas](https://spotonplumbing.com/service-areas/)
        Serving Tulsa, Broken Arrow, or Sapulpa.
        Copyright 2026 Spot On Plumbing
        """

        facts = build_page_facts(content, {"name": "Spot On Plumbing"})

        self.assertEqual(facts["service_areas"], ["Tulsa", "Broken Arrow", "Sapulpa"])
        self.assertNotIn("https", facts["service_areas"])
        self.assertNotIn("spotonplumbing", facts["service_areas"])
        self.assertNotIn("2026 Spot On Pl", facts["addresses"])

    def test_page_facts_recover_plumbingbo_name_from_visible_raw_sources(self):
        samples = (
            ("PlumbingBO is a leading plumbing company.", "visible_self_identification"),
            ("@ 2024 PlumbingBO - Local Plumbing Services", "visible_footer_owner"),
            ("![PlumbingBO](https://example.com/assets/logo.png)", "visible_logo_alt"),
        )

        for content, source in samples:
            with self.subTest(source=source):
                facts = build_page_facts(content)
                self.assertEqual(facts["business_names"], ["PlumbingBO"])
                self.assertEqual(
                    facts["observations"]["business_names"],
                    [{"value": "PlumbingBO", "source": source, "scope": "target_page"}],
                )

    def test_page_facts_preserve_raw_values_and_sources_for_all_l3_fields(self):
        facts = build_page_facts(
            """
            PlumbingBO is a leading plumbing company.
            Call +1 (315) 228-9299.
            153 7th Avenue, New York, NY 10011
            Serving New York, Brooklyn and Queens.
            """
        )

        self.assertEqual(facts["business_names"], ["PlumbingBO"])
        self.assertIn("+1 (315) 228-9299", facts["phones"])
        self.assertIn("153 7th Avenue, New York, NY 10011", facts["addresses"])
        self.assertEqual(facts["service_areas"], ["New York", "Brooklyn", "Queens"])
        for field in ("business_names", "phones", "addresses", "service_areas"):
            self.assertEqual(
                facts[field],
                [item["value"] for item in facts["observations"][field]],
            )

    def test_visible_identity_signal_outranks_weak_domain_name(self):
        facts = build_page_facts(
            "",
            None,
            [
                {
                    "field": "name",
                    "value": "plumbingbo.com",
                    "source": "json_ld",
                    "quality": "weak",
                    "scope": "target_page",
                },
                {
                    "field": "name",
                    "value": "PlumbingBO",
                    "source": "logo_alt",
                    "quality": "supporting",
                    "scope": "target_page",
                },
            ],
        )

        self.assertEqual(facts["business_names"], ["PlumbingBO"])
        self.assertEqual(facts["observations"]["business_names"][0]["source"], "logo_alt")
