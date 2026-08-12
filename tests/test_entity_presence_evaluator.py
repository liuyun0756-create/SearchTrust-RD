import unittest

from app.report_v21.entity_presence_evaluator import evaluate_entity_presence_rules
from app.report_v21.evidence_ledger import build_evidence_ledger, build_layer_evidence
from app.report_v21.page_facts import build_page_facts


class EntityPresenceEvaluatorTests(unittest.TestCase):
    def test_rapid_rooter_hours_are_present_even_when_page_schedules_conflict(self):
        content = """
        Rapid Rooter is a local plumbing company.
        Mon-Sat: 24-Hours Emergency Service - Call (559) 440-9314
        Serving Fresno, Madera Ranchos, Sanger and Clovis.
        Mon - Sat: 12:00 AM - 12:00 PM
        Sun: Closed
        """
        facts = build_page_facts(content, {"name": "Rapid Rooter"})

        results, applicability, payload = evaluate_entity_presence_rules(facts)

        self.assertFalse(results[21])
        self.assertTrue(results[22])
        self.assertFalse(results[23])
        self.assertFalse(results[24])
        self.assertFalse(results[25])
        self.assertTrue(all(applicability.values()))
        self.assertEqual(payload["rule_results"]["rule_25"], False)
        self.assertGreaterEqual(
            len(payload["findings"]["rule_25"]["page_observations"]),
            2,
        )

    def test_rule_22_requires_three_address_components(self):
        incomplete = build_page_facts("Visit us at 171 Attorney St")
        complete = build_page_facts("171 Attorney St, New York, NY 10002")

        incomplete_results, _, _ = evaluate_entity_presence_rules(incomplete)
        complete_results, _, _ = evaluate_entity_presence_rules(complete)

        self.assertTrue(incomplete_results[22])
        self.assertFalse(complete_results[22])

    def test_empty_page_triggers_all_five_presence_rules(self):
        results, applicability, payload = evaluate_entity_presence_rules(
            build_page_facts("")
        )

        self.assertEqual(results, {21: True, 22: True, 23: True, 24: True, 25: True})
        self.assertEqual(
            applicability,
            {21: True, 22: True, 23: True, 24: True, 25: True},
        )
        self.assertTrue(all(
            finding["condition"] == "missing"
            for finding in payload["findings"].values()
        ))

    def test_missing_l2_evidence_does_not_promote_nearby_generic_copy(self):
        facts = build_page_facts("Line Location")
        _, _, payload = evaluate_entity_presence_rules(facts)
        context = {
            "url": "https://example.com/service",
            "content": "Line Location",
            "page_facts": facts,
            "backend_entity_presence": payload,
        }

        evidence = build_layer_evidence(
            [22],
            build_evidence_ledger(context),
            context,
        )

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["comparison_result"], "missing")
        self.assertIsNone(evidence[0]["extracted_text"])
        self.assertNotIn("Line Location", str(evidence[0]))
