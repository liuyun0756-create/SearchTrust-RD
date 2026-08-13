import unittest

from app.report_v21.entity_presence_evaluator import evaluate_entity_presence_rules
from app.report_v21.evidence_ledger import build_evidence_ledger, build_layer_evidence
from app.report_v21.gbp_rule_evaluator import evaluate_gbp_rules
from app.report_v21.page_facts import build_page_facts


class EntityPresenceEvaluatorTests(unittest.TestCase):
    SPOT_ON_HTML = """
    <main>
      <p>Spot On Plumbing offers emergency plumbing services to homeowners
      experiencing issues across Tulsa, Oklahoma. If you notice any issues in
      your Tulsa, Broken Arrow, Catoosa, Sapulpa, Owasso, Sand Springs, Bixby,
      or Glenpool home, let us know right away!</p>
    </main>
    <footer><ul class="elementor-icon-list-items">
      <li><a href="https://goo.gl/maps/example"><span>1911 West Reno Street</span></a></li>
      <li><a href="https://goo.gl/maps/example"><span>Broken Arrow, OK 74012</span></a></li>
    </ul></footer>
    """

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

    def test_spot_on_split_address_and_explicit_city_list_are_unified_page_facts(self):
        page_facts = build_page_facts(
            self.SPOT_ON_HTML,
            structured_content=self.SPOT_ON_HTML,
        )

        results, _, payload = evaluate_entity_presence_rules(page_facts)

        self.assertFalse(results[22])
        self.assertFalse(results[24])
        address = payload["findings"]["rule_22"]["page_observations"][0]
        service_areas = payload["findings"]["rule_24"]["page_observations"]
        self.assertEqual(address["raw_value"], "1911 West Reno Street Broken Arrow, OK 74012")
        self.assertEqual(
            page_facts["service_areas"],
            [
                "Tulsa", "Broken Arrow", "Catoosa", "Sapulpa", "Owasso",
                "Sand Springs", "Bixby", "Glenpool",
            ],
        )
        self.assertTrue(address["eligible_for_l3"])
        self.assertTrue(all(item["eligible_for_l3"] for item in service_areas))

    def test_spot_on_l2_and_l3_share_the_same_complete_page_facts(self):
        page_facts = build_page_facts(
            self.SPOT_ON_HTML,
            structured_content=self.SPOT_ON_HTML,
        )
        l2_results, _, _ = evaluate_entity_presence_rules(page_facts)
        l3_results, _, l3_findings = evaluate_gbp_rules({
            "url": "https://spotonplumbing.com/emergency-services/",
            "content_checked": True,
            "gbp_lookup_attempted": True,
            "gbp_data": {
                "name": "Spot On Plumbing",
                "address": "1911 West Reno Street, Broken Arrow, OK 74012",
                "phone": "(918) 818-3901",
                "service_areas": [
                    "Tulsa", "Broken Arrow", "Catoosa", "Sapulpa", "Owasso",
                    "Sand Springs", "Bixby", "Glenpool",
                ],
                "service_areas_observed": True,
            },
            "page_facts": page_facts,
        })

        self.assertFalse(l2_results[22])
        self.assertFalse(l2_results[24])
        self.assertFalse(l3_results[27])
        self.assertFalse(l3_results[29])
        self.assertEqual(l3_findings["rule_27"]["condition"], "semantic_match")
        self.assertEqual(l3_findings["rule_29"]["condition"], "exact_match")
        self.assertEqual(l3_findings["rule_27"]["page_values"], page_facts["addresses"])
        self.assertEqual(l3_findings["rule_29"]["page_values"], page_facts["service_areas"])

        strict_results, _, strict_findings = evaluate_gbp_rules({
            "url": "https://spotonplumbing.com/emergency-services/",
            "content_checked": True,
            "gbp_lookup_attempted": True,
            "gbp_data": {
                "name": "Spot On Plumbing",
                "address": "1911 W Reno St, Broken Arrow, OK 74012",
                "phone": "(918) 818-3901",
                "service_areas": [
                    "Tulsa", "Broken Arrow", "Catoosa", "Sapulpa", "Owasso",
                    "Sand Springs", "Bixby",
                ],
                "service_areas_observed": True,
            },
            "page_facts": page_facts,
        })
        self.assertFalse(strict_results[27])
        self.assertFalse(strict_results[29])
        self.assertEqual(strict_findings["rule_27"]["condition"], "semantic_match")
        self.assertEqual(strict_findings["rule_29"]["condition"], "compatible_difference")

    def test_art_douglas_rejects_prose_way_and_composes_labeled_address_block(self):
        page_facts = build_page_facts("""
        The tech arrived in 20 minutes and tried there best to fix it the cheapest way
        Address:
        5855 E Clinton Ave.
        Fresno, CA 93727
        """)

        l2_results, _, l2_findings = evaluate_entity_presence_rules(page_facts)
        exact_results, _, exact_findings = evaluate_gbp_rules({
            "url": "https://www.artdouglasplumbing.com/drain-cleaning",
            "content_checked": True,
            "gbp_lookup_attempted": True,
            "gbp_data": {
                "name": "Art Douglas Plumbing",
                "address": "5855 E Clinton Ave., Fresno, CA 93727",
            },
            "page_facts": page_facts,
        })
        strict_results, _, strict_findings = evaluate_gbp_rules({
            "url": "https://www.artdouglasplumbing.com/drain-cleaning",
            "content_checked": True,
            "gbp_lookup_attempted": True,
            "gbp_data": {
                "name": "Art Douglas Plumbing",
                "address": "5855 E Clinton Ave, Fresno, CA 93727",
            },
            "page_facts": page_facts,
        })

        self.assertEqual(
            page_facts["addresses"],
            ["5855 E Clinton Ave. Fresno, CA 93727"],
        )
        prose_candidate = next(
            item
            for item in page_facts["candidate_observations"]["addresses"]
            if str(item.get("value") or "").startswith("20 minutes")
        )
        self.assertEqual(prose_candidate["validation"], "rejected")
        self.assertEqual(prose_candidate["rejection_reason"], "not_street_address")
        self.assertFalse(prose_candidate["eligible_for_l3"])
        self.assertFalse(l2_results[22])
        self.assertEqual(l2_findings["findings"]["rule_22"]["condition"], "present")
        self.assertFalse(exact_results[27])
        self.assertEqual(exact_findings["rule_27"]["condition"], "semantic_match")
        self.assertFalse(strict_results[27])
        self.assertEqual(strict_findings["rule_27"]["condition"], "semantic_match")
