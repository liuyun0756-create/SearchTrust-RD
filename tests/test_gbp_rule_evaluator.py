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

    def test_missing_gbp_snapshot_triggers_comparison_rules_without_claiming_match(self):
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

        self.assertTrue(all(results.values()))
        self.assertTrue(all(applicability.values()))
        self.assertTrue(all(item["condition"] == "gbp_unavailable" for item in findings.values()))

    def test_profile_without_identity_fields_is_not_treated_as_checked(self):
        results, _, findings = evaluate_gbp_rules(_context(
            {"business_names": [], "addresses": [], "phones": [], "service_areas": []},
            {"rating": 4.9, "reviews": 100},
        ))

        self.assertTrue(all(results.values()))
        self.assertTrue(all(item["condition"] == "gbp_unavailable" for item in findings.values()))

    def test_unverified_profile_does_not_claim_a_missing_service_area(self):
        results, _, findings = evaluate_gbp_rules(_context(
            {
                "business_names": [],
                "addresses": [],
                "phones": [],
                "service_areas": ["Tulsa"],
            },
            {"rating": 4.9, "reviews": 100},
        ))

        self.assertTrue(results[29])
        self.assertEqual(findings["rule_29"]["condition"], "gbp_unavailable")


if __name__ == "__main__":
    unittest.main()
