import unittest

from app.report_v21.evidence_ledger import build_layer_evidence
from app.report_v21.quality import prune_unsupported_evidence


class EvidenceQualityTests(unittest.TestCase):
    def test_structured_page_name_and_verified_missing_gbp_field_survive_pruning(self):
        context = {
            "url": "https://example.com/service/",
            "gbp_url": "https://maps.example/profile",
            "content": "Visible service copy without the logo business name.",
            "page_facts": {
                "business_names": ["Example Plumbing LLC."],
                "addresses": [],
                "phones": ["(918) 555-0100"],
                "service_areas": [],
            },
            "page_business": {"name": "Example Plumbing LLC."},
            "gbp_data": {"name": "Example Plumbing, LLC"},
            "backend_gbp_findings": {
                "rule_26": {
                    "condition": "mismatch",
                    "page_values": ["Example Plumbing LLC."],
                    "gbp_values": ["Example Plumbing, LLC"],
                    "explanation": "Names differ.",
                },
                "rule_28": {
                    "condition": "gbp_field_missing",
                    "page_values": ["(918) 555-0100"],
                    "gbp_values": [],
                    "explanation": "GBP phone was not returned.",
                },
            },
        }
        report = {
            "gbp_status": {"status": "checked"},
            "layers": [{
                "layer_key": "entity_consistency",
                "evidence_items": build_layer_evidence([26, 28], {}, context),
            }],
            "key_issues": [],
        }

        warnings = prune_unsupported_evidence(report, context)
        ids = {item["id"] for item in report["layers"][0]["evidence_items"]}

        self.assertIn("ev-rule-26-page-01", ids)
        self.assertIn("ev-rule-26-gbp-01", ids)
        self.assertIn("ev-rule-28-page-01", ids)
        self.assertIn("ev-rule-28-gbp-missing", ids)
        self.assertFalse(warnings)


if __name__ == "__main__":
    unittest.main()
