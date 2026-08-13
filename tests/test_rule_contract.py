import unittest

from app.report_v21.gbp_rule_evaluator import evaluate_gbp_rules
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

    def test_backend_overrides_presence_rules_and_dify_may_omit_them(self):
        results = _complete_vector(False)
        applicability = _complete_vector(True)
        for rule_id in (21, 22, 23, 24, 25):
            results.pop(f"rule_{rule_id}")
            applicability.pop(f"rule_{rule_id}")

        parsed_results, parsed_applicability = parse_rule_results(
            {
                "rule_results": results,
                "rule_applicability": applicability,
            },
            backend_presence_results={
                21: False, 22: True, 23: False, 24: False, 25: False,
            },
            backend_presence_applicability={
                21: True, 22: True, 23: True, 24: True, 25: True,
            },
        )

        self.assertFalse(parsed_results[21])
        self.assertTrue(parsed_results[22])
        self.assertFalse(parsed_results[25])
        self.assertTrue(all(parsed_applicability[rule_id] for rule_id in range(21, 26)))

    def test_backend_can_own_presence_and_gbp_rules_together(self):
        results = _complete_vector(False)
        applicability = _complete_vector(True)
        for rule_id in range(21, 30):
            results.pop(f"rule_{rule_id}")
            applicability.pop(f"rule_{rule_id}")

        parsed_results, _ = parse_rule_results(
            {
                "rule_results": results,
                "rule_applicability": applicability,
                "rule_errors": [
                    "rule_25 old LLM node returned invalid JSON",
                    "rule_28 old LLM node returned invalid JSON",
                ],
            },
            backend_presence_results={rule_id: False for rule_id in range(21, 26)},
            backend_presence_applicability={rule_id: True for rule_id in range(21, 26)},
            backend_gbp_results={rule_id: False for rule_id in range(26, 30)},
            backend_gbp_applicability={rule_id: True for rule_id in range(26, 30)},
        )

        self.assertTrue(all(parsed_results[rule_id] is False for rule_id in range(21, 30)))

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

    def test_page_facts_do_not_turn_service_narrative_into_area_values(self):
        content = (
            "Serving homeowners and businesses in Manhattan, NY and the "
            "following communities in and around Manhattan and the entire "
            "New York metro area.\n"
            "Serving the entire New York metro area, Including:"
        )

        facts = build_page_facts(content)

        self.assertEqual(facts["service_areas"], [])

    def test_page_facts_recover_plumbingbo_name_from_visible_raw_sources(self):
        samples = (
            ("PlumbingBO is a leading plumbing company.", "page.dom.self_identification"),
            ("@ 2024 PlumbingBO - Local Plumbing Services", "page.dom.footer_owner"),
            ("![PlumbingBO](https://example.com/assets/logo.png)", "page.dom.logo_alt"),
        )

        for content, source in samples:
            with self.subTest(source=source):
                facts = build_page_facts(content)
                self.assertEqual(facts["business_names"], ["PlumbingBO"])
                observation = facts["observations"]["business_names"][0]
                self.assertEqual(observation["source_type"], source)
                self.assertEqual(observation["source_scope"], "target_page")
                self.assertEqual(observation["validation"], "valid")
                self.assertTrue(observation["eligible_for_l3"])

    def test_link_wrapped_logo_does_not_leak_markdown_into_business_name(self):
        facts = build_page_facts(
            "[![Art Douglas Plumbing Inc.](https://example.com/logo.png)](/)",
            identity_signals=[{
                "field": "name",
                "value": "Art Douglas Plumbing Inc.",
                "source": "logo_alt",
                "quality": "supporting",
                "scope": "target_page",
            }],
        )

        self.assertEqual(facts["business_names"], ["Art Douglas Plumbing Inc."])
        self.assertFalse(any(
            str(item.get("value") or "").startswith("![")
            for item in facts["candidate_observations"]["business_names"]
        ))

        results, _, findings = evaluate_gbp_rules({
            "url": "https://www.artdouglasplumbing.com/drain-cleaning",
            "content_checked": True,
            "gbp_lookup_attempted": True,
            "gbp_data": {
                "name": "Art Douglas Plumbing Inc",
                "website": "https://www.artdouglasplumbing.com/",
            },
            "page_facts": facts,
        })
        # The extraction bug is gone. The final period still differs and must
        # remain a mismatch under the unchanged strict L3 name contract.
        self.assertTrue(results[26])
        self.assertEqual(findings["rule_26"]["condition"], "mismatch")

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
        self.assertEqual(facts["observations"]["business_names"][0]["source_type"], "page.dom.logo_alt")

    def test_page_facts_block_url_numbers_malformed_phones_and_generic_names(self):
        content = """
        # Pipe replacement for homeowners
        [Facebook](https://www.facebook.com/people/Waterhouse/100068143147443/)
        Call [(212) 777-3003](tel:+12127773003)
        Phone: 202.787-.4145
        """

        facts = build_page_facts(
            content,
            {"name": "for", "phone": "202.787-.4145"},
            source_url="https://www.waterhouse.nyc/pipe-replacement",
        )

        self.assertEqual(facts["business_names"], [])
        self.assertEqual(facts["phones"], ["(212) 777-3003"])
        phone = facts["observations"]["phones"][0]
        self.assertEqual(phone["source_type"], "page.dom.tel_href")
        self.assertEqual(phone["normalized_value"], "+12127773003")
        self.assertEqual(phone["source_url"], "https://www.waterhouse.nyc/pipe-replacement")
        all_values = [
            item["value"]
            for items in facts["candidate_observations"].values()
            for item in items
        ]
        self.assertNotIn("100068143147443", all_values)
        self.assertTrue(any(
            item.get("rejection_reason") == "generic_name"
            for item in facts["rejected_observations"]["business_names"]
        ))

    def test_page_facts_classify_structured_sources_for_all_l3_fields(self):
        structured = """
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Plumber",
          "name": "Example Plumbing",
          "telephone": "+12125550100",
          "address": {
            "@type": "PostalAddress",
            "streetAddress": "10 Main St",
            "addressLocality": "New York",
            "addressRegion": "NY",
            "postalCode": "10001"
          },
          "areaServed": [{"@type": "City", "name": "New York"}, "Brooklyn"]
        }
        </script>
        """

        facts = build_page_facts("Visible service page", structured_content=structured)

        self.assertEqual(facts["business_names"], ["Example Plumbing"])
        self.assertEqual(facts["phones"], ["+12125550100"])
        self.assertEqual(facts["addresses"], ["10 Main St, New York, NY, 10001"])
        self.assertEqual(facts["service_areas"], ["New York", "Brooklyn"])
        expected_sources = {
            "business_names": "page.jsonld.local_business.name",
            "phones": "page.jsonld.telephone",
            "addresses": "page.jsonld.postal_address",
            "service_areas": "page.jsonld.area_served",
        }
        for field, source_type in expected_sources.items():
            self.assertTrue(all(
                item["source_type"] == source_type
                and item["eligible_for_l3"] is True
                for item in facts["observations"][field]
            ))

    def test_jsonld_address_components_do_not_create_duplicate_commas(self):
        structured = """
        <script type="application/ld+json">
        {
          "@type": "Plumber",
          "name": "Roto-Rooter",
          "address": {
            "@type": "PostalAddress",
            "streetAddress": "450 7th Ave Ste B, ",
            "addressLocality": "New York",
            "addressRegion": "NY",
            "postalCode": "10123",
            "addressCountry": "US"
          }
        }
        </script>
        """

        facts = build_page_facts("", structured_content=structured)

        self.assertEqual(
            facts["addresses"],
            ["450 7th Ave Ste B, New York, NY, 10123, US"],
        )

    def test_secondary_phone_is_audited_but_not_compared_as_primary(self):
        facts = build_page_facts("""
        Phone: (212) 777-3003
        Fax: (212) 777-3004
        """)

        self.assertEqual(facts["phones"], ["(212) 777-3003"])
        self.assertTrue(any(
            item.get("value") == "(212) 777-3004"
            and item.get("rejection_reason") == "secondary_phone_not_primary_identity"
            for item in facts["rejected_observations"]["phones"]
        ))

    def test_waterhouse_brand_outranks_third_party_review_logo(self):
        facts = build_page_facts("""
        Pipe Replacement | WaterHouse Plumbing | New York City, NY
        Waterhouse Plumbing, the top plumbing services in New York City.
        ![Google : colorful logo](https://cdn.example/google-icon.svg)
        Contact [Waterhouse Plumbing](/)
        [Call (212) 777-3003](tel:+12127773003)
        https://www.facebook.com/people/Waterhouse-Plumbing/100068143147443/
        """)

        self.assertEqual(facts["business_names"], ["Waterhouse Plumbing"])
        self.assertEqual(facts["phones"], ["(212) 777-3003"])
        self.assertTrue(any(
            item.get("value") == "Google : colorful"
            and item.get("rejection_reason") == "third_party_or_descriptor_logo"
            for item in facts["rejected_observations"]["business_names"]
        ))

    def test_corroborated_plumbingbo_brand_outranks_conflicting_jsonld_titles(self):
        content = """
        PlumbingBO is a leading plumbing company.
        @ 2024 PlumbingBO - Local Plumbing Services
        <script type="application/ld+json">
        [
          {"@type":"Plumber","name":"Plumbing Services - PlumbingBO"},
          {"@type":"Organization","name":"PlumbingBO - Plumbing Services"}
        ]
        </script>
        """

        facts = build_page_facts(content)

        self.assertEqual(facts["business_names"], ["PlumbingBO"])
        self.assertTrue(all(
            item.get("eligible_for_l3") is False
            for item in facts["rejected_observations"]["business_names"]
            if item.get("source_type") == "page.jsonld.local_business.name"
        ))
