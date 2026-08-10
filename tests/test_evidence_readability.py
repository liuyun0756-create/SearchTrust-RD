import unittest

from app.report_v21.evidence_ledger import build_evidence_ledger, build_layer_evidence
from app.report_v21.quality import prune_unsupported_evidence


class EvidenceReadabilityTests(unittest.TestCase):
    def context(self):
        content = "\n".join([
            "## [![plumbing contractor serving tri cities washington](https://example.com/wp-content/uploads/plumber.jpg)](https://example.com/)",
            "[Get a Quote](https://example.com/quote)",
            "[Call (509) 940-7811 for 24/7 Emergency Service](tel:5099407811)",
            "Serving Tri-Cities Washington and nearby communities.",
            "Our technicians assess the issue and provide a quote before repair.",
            "![Google](data:image/svg+xml,%3Csvg%20viewBox%3D%220%200%22%3E)",
        ])
        return {
            "url": "https://example.com/service/",
            "content": content,
            "page_content": content,
        }

    def test_rule_specific_evidence_uses_readable_page_observations(self):
        context = self.context()
        ledger = build_evidence_ledger(context)

        cta = build_layer_evidence([8], ledger, context)[0]
        geography = build_layer_evidence([30], ledger, context)[0]
        responsibility = build_layer_evidence([9], ledger, context)[0]
        imagery = build_layer_evidence([6], ledger, context)[0]

        self.assertEqual(cta["source_label"], "Calls to action")
        self.assertIn("Get a Quote", cta["extracted_text"])
        self.assertIn("Call (509) 940-7811", cta["extracted_text"])
        self.assertEqual(
            geography["extracted_text"],
            "Serving Tri-Cities Washington and nearby communities.",
        )
        self.assertIn("Our technicians assess the issue", responsibility["extracted_text"])
        self.assertEqual(imagery["extracted_text"], "plumbing contractor serving tri cities washington")
        for item in (cta, geography, responsibility, imagery):
            value = str(item.get("extracted_text") or "")
            self.assertNotIn("wp-content", value)
            self.assertNotIn("data:image", value)
            self.assertNotIn("viewBox", value)

    def test_missing_fallback_is_short_and_does_not_repeat_scope_prefix(self):
        context = {
            "url": "https://example.com/service/",
            "content": "General information about the company.",
            "page_content": "General information about the company.",
        }
        evidence = build_layer_evidence([22], build_evidence_ledger(context), context)[0]

        self.assertEqual(evidence["source_label"], "Address")
        self.assertEqual(
            evidence["normalized_value"],
            "Not found: a visible street address, such as the business's customer-facing service location.",
        )
        self.assertNotIn("Not found in the checked scope", evidence["normalized_value"])

    def test_each_triggered_rule_keeps_its_own_evidence_when_excerpt_is_shared(self):
        content = (
            "Our technicians handle the service request, explain limitations, "
            "and provide warranty follow up."
        )
        context = {
            "url": "https://example.com/service/",
            "content": content,
            "page_content": content,
        }

        evidence = build_layer_evidence(
            [9, 10, 12],
            build_evidence_ledger(context),
            context,
        )

        self.assertEqual(len(evidence), 3)
        self.assertEqual(
            {item["id"].split("-")[2] for item in evidence},
            {"9", "10", "12"},
        )

    def test_missing_rules_use_distinct_fixed_explanations(self):
        context = {
            "url": "https://example.com/service/",
            "content": "General information about plumbing services.",
            "page_content": "General information about plumbing services.",
        }

        evidence = build_layer_evidence(
            [9, 10, 12],
            build_evidence_ledger(context),
            context,
        )

        self.assertEqual(len(evidence), 3)
        self.assertEqual(
            [item["source_label"] for item in evidence],
            ["Operational responsibility", "Limits and complex cases", "Outcome and follow-up"],
        )
        for item in evidence:
            self.assertTrue(str(item["normalized_value"]).startswith("Not found:"))
        self.assertIn("who handles the service request", evidence[0]["normalized_value"])
        self.assertIn("exclusions", evidence[1]["normalized_value"])
        self.assertIn("return visit", evidence[2]["normalized_value"])

    def test_missing_rule_evidence_is_backfilled_without_discarding_real_evidence(self):
        content = "Our Service Area\nServing Tri-Cities Washington."
        context = {
            "url": "https://example.com/service/",
            "content": content,
            "page_content": content,
        }

        evidence = build_layer_evidence(
            [3, 31, 33],
            build_evidence_ledger(context),
            context,
        )

        self.assertGreaterEqual(len(evidence), 3)
        evidence_rules = {
            item["id"].split("-")[2]
            for item in evidence
            if item["id"].startswith("ev-rule-")
        }
        self.assertEqual(evidence_rules, {"3", "31", "33"})

    def test_multiple_real_excerpts_for_one_rule_are_all_preserved(self):
        context = {
            "url": "https://example.com/service/",
            "content": "Customer reviews",
            "page_content": "Customer reviews",
            "review_corpus": [
                {"id": "review-1", "text": "Fast shower installation."},
                {"id": "review-2", "text": "Professional drain repair."},
                {"id": "review-3", "text": "Clear explanation and timely service."},
            ],
        }

        evidence = build_layer_evidence(
            [38],
            build_evidence_ledger(context),
            context,
        )

        self.assertEqual(len(evidence), 3)

    def test_cleaned_multi_line_context_survives_traceability_pruning(self):
        context = self.context()
        report = {
            "gbp_status": {"status": "not_checked"},
            "layers": [{
                "layer_key": "specificity",
                "evidence_items": build_layer_evidence(
                    [8],
                    build_evidence_ledger(context),
                    context,
                ),
            }],
            "key_issues": [],
        }

        warnings = prune_unsupported_evidence(report, context)

        self.assertFalse(warnings)
        self.assertEqual(len(report["layers"][0]["evidence_items"]), 1)


if __name__ == "__main__":
    unittest.main()
