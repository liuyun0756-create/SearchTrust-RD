import unittest
from unittest.mock import patch

from app.report_v21.entity_presence_evaluator import evaluate_entity_presence_rules
from app.report_v21.gbp_rule_evaluator import evaluate_gbp_rules
from app.report_v21.page_facts import build_page_facts


def _l3(page_facts, address):
    return evaluate_gbp_rules({
        "url": "https://example.com/service",
        "content_checked": True,
        "gbp_lookup_attempted": True,
        "gbp_data": {"name": "Example Plumbing", "address": address},
        "page_facts": page_facts,
    })


class AddressFactPipelineTests(unittest.TestCase):
    def test_labeled_dom_block_forms_one_complete_address_and_rejects_prose(self):
        visible = """
        The tech arrived in 20 minutes and tried there best to fix it the cheapest way
        Address:
        5855 E Clinton Ave.
        Fresno, CA 93727
        """
        structured = """
        <main><p>The tech arrived in 20 minutes and tried there best to fix it the cheapest way</p></main>
        <footer><div class="address-column">
          <p>Address:</p><p>5855 E Clinton Ave.</p><p>Fresno, CA 93727</p>
          <p>Fresno/Clovis:</p><a href="tel:559-291-7230">559-291-7230</a>
        </div></footer>
        """

        facts = build_page_facts(visible, structured_content=structured)

        self.assertEqual(facts["addresses"], ["5855 E Clinton Ave. Fresno, CA 93727"])
        accepted = facts["observations"]["addresses"][0]
        self.assertEqual(accepted["source_type"], "page.dom.labeled_address_block")
        self.assertEqual(accepted["components"]["city"], "Fresno")
        prose = next(
            item
            for item in facts["rejected_observations"]["addresses"]
            if str(item.get("raw_value") or "").startswith("20 minutes")
        )
        self.assertEqual(prose["rejection_reason"], "not_street_address")
        trailing = next(
            item
            for item in facts["rejected_observations"]["addresses"]
            if str(item.get("raw_value") or "").endswith("Fresno/Clovis:")
        )
        self.assertEqual(
            trailing["rejection_reason"],
            "unexpected_trailing_content",
        )

    def test_labeled_markdown_block_stops_before_phone_and_matches_semantically(self):
        visible = """
        Address:
        5855 E Clinton Ave.
        Fresno, CA 93727
        Fresno/Clovis:**559-291-7230**
        Madera:**559-661-1060**
        """

        facts = build_page_facts(visible)
        results, _, findings = _l3(
            facts,
            "5855 E Clinton Ave, Fresno, CA 93727",
        )

        self.assertEqual(
            facts["addresses"],
            ["5855 E Clinton Ave. Fresno, CA 93727"],
        )
        self.assertNotIn(
            "559-291-7230",
            " ".join(facts["addresses"]),
        )
        self.assertFalse(results[27])
        self.assertEqual(findings["rule_27"]["condition"], "semantic_match")

    def test_parser_rejects_unlabelled_trailing_unit_but_accepts_real_suite(self):
        contaminated = build_page_facts(
            "5855 E Clinton Ave., Fresno, CA 93727, Fresno/Clovis:**559-291-7230**"
        )
        suite = build_page_facts("10 Main St Suite 200, Austin, TX 78701")

        self.assertEqual(contaminated["addresses"], [])
        self.assertTrue(any(
            item.get("rejection_reason") == "unexpected_trailing_content"
            for item in contaminated["rejected_observations"]["addresses"]
        ))
        self.assertEqual(
            suite["addresses"],
            ["10 Main St Suite 200, Austin, TX 78701"],
        )

    def test_shared_map_target_associates_visible_address_nodes(self):
        structured = """
        <footer>
          <a href="https://goo.gl/maps/example">1911 West Reno Street</a>
          <a href="https://goo.gl/maps/example">Broken Arrow, OK 74012</a>
        </footer>
        """

        facts = build_page_facts("", structured_content=structured)

        self.assertEqual(
            facts["addresses"],
            ["1911 West Reno Street Broken Arrow, OK 74012"],
        )
        self.assertEqual(
            facts["observations"]["addresses"][0]["source_type"],
            "page.dom.map_address",
        )

    def test_hidden_map_url_does_not_become_a_page_address(self):
        structured = """
        <a href="https://maps.google.com/?q=10+Main+St+Austin+TX">Directions</a>
        """

        facts = build_page_facts("", structured_content=structured)

        self.assertEqual(facts["addresses"], [])
        rejected = facts["rejected_observations"]["addresses"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["rejection_reason"], "hidden_map_url_only")

    def test_complete_single_line_without_zip_satisfies_l2(self):
        facts = build_page_facts("10 Main St, Austin, TX")
        results, _, _ = evaluate_entity_presence_rules(facts)

        self.assertEqual(facts["addresses"], ["10 Main St, Austin, TX"])
        self.assertFalse(results[22])
        self.assertEqual(
            facts["observations"]["addresses"][0]["completeness"],
            "postal_partial",
        )

    def test_multiple_visible_locations_are_all_retained(self):
        facts = build_page_facts("""
        10 Main St, Austin, TX 78701
        20 Oak Ave., Dallas, TX 75201
        """)

        self.assertEqual(
            facts["addresses"],
            ["10 Main St, Austin, TX 78701", "20 Oak Ave., Dallas, TX 75201"],
        )

    def test_raw_variants_are_preserved_and_l3_compares_address_semantics(self):
        facts = build_page_facts("""
        5855 E Clinton Ave., Fresno, CA 93727
        5855 E Clinton Ave, Fresno, CA 93727
        """)
        results, _, findings = _l3(facts, "5855 E Clinton Ave, Fresno, CA 93727")

        self.assertEqual(len(facts["addresses"]), 2)
        self.assertFalse(results[27])
        self.assertEqual(findings["rule_27"]["condition"], "semantic_match")

    def test_jsonld_components_are_accepted_without_visible_text(self):
        structured = """
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Plumber",
          "name": "Example Plumbing",
          "address": {
            "@type": "PostalAddress",
            "streetAddress": "10 Main St",
            "addressLocality": "Austin",
            "addressRegion": "TX",
            "postalCode": "78701"
          }
        }
        </script>
        """

        facts = build_page_facts("", structured_content=structured)

        self.assertEqual(facts["addresses"], ["10 Main St, Austin, TX, 78701"])
        self.assertEqual(
            facts["observations"]["addresses"][0]["source_type"],
            "page.jsonld.postal_address",
        )

    def test_postal_microdata_is_parsed_as_structured_address(self):
        structured = """
        <div itemprop="address" itemscope itemtype="https://schema.org/PostalAddress">
          <span itemprop="streetAddress">10 Main St</span>
          <span itemprop="addressLocality">Austin</span>
          <span itemprop="addressRegion">TX</span>
          <span itemprop="postalCode">78701</span>
        </div>
        """

        facts = build_page_facts("", structured_content=structured)

        self.assertEqual(facts["addresses"], ["10 Main St, Austin, TX, 78701"])
        self.assertEqual(
            facts["observations"]["addresses"][0]["source_type"],
            "page.microdata.postal_address",
        )

    def test_non_us_free_text_fails_closed_but_structured_non_us_is_retained(self):
        free_text = build_page_facts("10 Downing Street, London, UK SW1A 2AA")
        structured = build_page_facts("", structured_content="""
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Organization",
          "name": "Example UK",
          "address": {
            "@type": "PostalAddress",
            "streetAddress": "10 Downing Street",
            "addressLocality": "London",
            "postalCode": "SW1A 2AA",
            "addressCountry": "GB"
          }
        }
        </script>
        """)

        self.assertEqual(free_text["addresses"], [])
        self.assertEqual(
            structured["addresses"],
            ["10 Downing Street, London, SW1A 2AA, GB"],
        )
        self.assertEqual(
            structured["observations"]["addresses"][0]["country_code"],
            "GB",
        )

    def test_parser_unavailable_fails_closed_for_text_but_keeps_structured_components(self):
        structured = """
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Plumber",
          "name": "Example Plumbing",
          "address": {
            "@type": "PostalAddress",
            "streetAddress": "10 Main St",
            "addressLocality": "Austin",
            "addressRegion": "TX"
          }
        }
        </script>
        """
        with patch("app.report_v21.us_address_parser.usaddress", None):
            text_facts = build_page_facts("10 Main St, Austin, TX")
            structured_facts = build_page_facts("", structured_content=structured)

        self.assertEqual(text_facts["addresses"], [])
        self.assertFalse(text_facts["address_diagnostic"]["parser_available"])
        self.assertEqual(structured_facts["addresses"], ["10 Main St, Austin, TX"])


if __name__ == "__main__":
    unittest.main()
