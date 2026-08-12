import unittest

from app.report_v21.gbp_rule_evaluator import evaluate_gbp_rules


def _context(page_facts, gbp_data, *, checked=True):
    return {
        "url": "https://example.com/service/",
        "input_gbp_url": "https://maps.google.com/example" if checked else "",
        "gbp_url": "https://maps.google.com/example" if checked else "",
        "gbp_lookup_attempted": checked,
        "page_facts": page_facts,
        "gbp_data": gbp_data,
    }


class GbpRuleEvaluatorTests(unittest.TestCase):
    def test_business_name_keeps_punctuation_and_case_strict(self):
        page_name = "Express 24 Hr Plumbing & Drain LLC."
        for gbp_name in (
            "Express 24 Hr Plumbing & Drain, LLC",
            "express 24 Hr Plumbing & Drain LLC.",
        ):
            results, _, findings = evaluate_gbp_rules(_context(
                {"business_names": [page_name], "phones": ["(509) 940-7811"]},
                {"name": gbp_name, "phone": "(509) 940-7811"},
            ))

            self.assertTrue(results[26])
            self.assertEqual(findings["rule_26"]["condition"], "mismatch")

        results, _, findings = evaluate_gbp_rules(_context(
            {"business_names": [page_name], "phones": ["(509) 940-7811"]},
            {"name": page_name, "phone": "(509) 940-7811"},
        ))
        self.assertFalse(results[26])
        self.assertEqual(findings["rule_26"]["condition"], "match")

    def test_exact_identity_comparison_is_deterministic(self):
        context = _context(
            {
                "business_names": ["Spot On Plumbing"],
                "addresses": ["1911 West Reno Street, Broken Arrow, OK 74012"],
                "phones": ["(918) 818-3901"],
                "service_areas": ["Tulsa", "Broken Arrow"],
            },
            {
                "name": "Spot On Plumbing of Tulsa Plumbers",
                "address": "1911 W Reno St, Broken Arrow, OK 74012",
                "phone": "+1 918-818-3901",
                "service_areas": ["Broken Arrow", "Tulsa"],
            },
        )

        runs = [evaluate_gbp_rules(context) for _ in range(3)]
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(runs[1], runs[2])
        results, applicability, findings = runs[0]
        self.assertEqual(results, {26: True, 27: True, 28: False, 29: False})
        self.assertTrue(all(applicability.values()))
        self.assertEqual(findings["rule_26"]["condition"], "mismatch")
        self.assertEqual(findings["rule_27"]["condition"], "mismatch")

    def test_plumbingbo_uses_raw_page_name_and_remains_strict(self):
        page_facts = {
            "business_names": ["PlumbingBO"],
            "addresses": [],
            "phones": [],
            "service_areas": [],
            "observations": {
                "business_names": [{
                    "value": "PlumbingBO",
                    "source": "visible_footer_owner",
                    "scope": "target_page",
                }],
            },
        }
        results, _, findings = evaluate_gbp_rules(_context(
            page_facts,
            {"name": "Local Plumbing Company - PlumbingBO", "phone": "(315) 228-9299"},
        ))

        self.assertTrue(results[26])
        self.assertEqual(findings["rule_26"]["condition"], "mismatch")
        self.assertEqual(findings["rule_26"]["page_values"], ["PlumbingBO"])
        self.assertEqual(
            findings["rule_26"]["page_observations"][0]["source"],
            "visible_footer_owner",
        )

    def test_storefront_service_area_is_not_applicable(self):
        results, applicability, findings = evaluate_gbp_rules(_context(
            {
                "business_names": ["Example Plumbing"],
                "addresses": [],
                "phones": [],
                "service_areas": [],
            },
            {
                "name": "Example Plumbing",
                "phone": "(918) 555-0100",
                "service_areas": [],
                "service_areas_observed": True,
                "service_area_business": False,
            },
        ))

        self.assertFalse(results[29])
        self.assertFalse(applicability[29])
        self.assertFalse(findings["rule_29"]["applicable"])
        self.assertEqual(findings["rule_29"]["condition"], "field_not_applicable")

    def test_missing_gbp_snapshot_does_not_trigger_comparison_rules(self):
        results, applicability, findings = evaluate_gbp_rules(_context(
            {
                "business_names": ["Spot On Plumbing"],
                "addresses": [],
                "phones": ["(918) 818-3901"],
                "service_areas": ["Tulsa"],
            },
            {},
            checked=False,
        ))

        self.assertFalse(any(results.values()))
        self.assertFalse(any(applicability.values()))
        self.assertTrue(all(item["condition"] == "gbp_unavailable" for item in findings.values()))

    def test_profile_without_identity_fields_is_not_treated_as_checked(self):
        results, applicability, findings = evaluate_gbp_rules(_context(
            {"business_names": [], "addresses": [], "phones": [], "service_areas": []},
            {"rating": 4.9, "reviews": 100},
        ))

        self.assertFalse(any(results.values()))
        self.assertFalse(any(applicability.values()))
        self.assertTrue(all(item["condition"] == "gbp_unavailable" for item in findings.values()))

    def test_unverified_profile_does_not_claim_a_missing_service_area(self):
        results, applicability, findings = evaluate_gbp_rules(_context(
            {
                "business_names": [],
                "addresses": [],
                "phones": [],
                "service_areas": ["Tulsa"],
            },
            {"rating": 4.9, "reviews": 100},
        ))

        self.assertFalse(results[29])
        self.assertFalse(applicability[29])
        self.assertEqual(findings["rule_29"]["condition"], "gbp_unavailable")

    def test_l3_ignores_rejected_or_non_target_page_observations(self):
        page_facts = {
            "version": "3",
            "business_names": ["Injected Supporting Brand"],
            "phones": ["(201) 878-8483"],
            "observations": {
                "business_names": [{
                    "value": "Injected Supporting Brand",
                    "source_type": "page.dom.visible_brand",
                    "scope": "site_discovery",
                    "validation": "valid",
                    "eligible_for_l3": True,
                }],
                "phones": [{
                    "value": "(201) 878-8483",
                    "source_type": "page.dom.visible_phone",
                    "scope": "target_page",
                    "validation": "rejected",
                    "eligible_for_l3": False,
                }],
            },
        }

        results, _, findings = evaluate_gbp_rules(_context(
            page_facts,
            {"name": "WaterHouse Plumbing Company", "phone": "(212) 777-3003"},
        ))

        self.assertTrue(results[26])
        self.assertTrue(results[28])
        self.assertEqual(findings["rule_26"]["condition"], "page_missing")
        self.assertEqual(findings["rule_28"]["condition"], "page_missing")
        self.assertEqual(findings["rule_26"]["page_values"], [])
        self.assertEqual(findings["rule_28"]["page_values"], [])

    def test_phone_formatting_is_canonical_but_digits_remain_strict(self):
        matching, _, finding = evaluate_gbp_rules(_context(
            {"phones": ["212.777.3003"]},
            {"name": "Example Plumbing", "phone": "(212) 777-3003"},
        ))
        different, _, _ = evaluate_gbp_rules(_context(
            {"phones": ["(201) 777-3003"]},
            {"name": "Example Plumbing", "phone": "(212) 777-3003"},
        ))

        self.assertFalse(matching[28])
        self.assertEqual(finding["rule_28"]["normalized_page_values"], ["+12127773003"])
        self.assertTrue(different[28])


if __name__ == "__main__":
    unittest.main()
