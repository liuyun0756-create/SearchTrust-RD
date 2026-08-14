import unittest

from app.report_v21.page_facts import build_page_facts
from app.report_v21.service_area_facts import (
    build_service_area_facts,
    compare_service_area_sets,
)


class ServiceAreaFactsTests(unittest.TestCase):
    def test_upper_restoration_confirms_places_and_rejects_serving_you(self):
        facts = build_page_facts(
            """
            We look forward to serving you!
            Upper Restoration serves all 5 boroughs of NYC, Long Island,
            New York, New Jersey, Connecticut and Pennsylvania. For all your
            restoration needs, call now to discuss appointments and estimates.
            """
        )

        self.assertEqual(
            facts["service_areas"],
            [
                "New York City",
                "Long Island",
                "New York",
                "New Jersey",
                "Connecticut",
                "Pennsylvania",
            ],
        )
        self.assertTrue(any(
            item.get("raw_value") == "you"
            and item.get("rejection_reason") == "pronoun_or_generic_not_place"
            and item.get("eligible_for_l3") is False
            for item in facts["rejected_observations"]["service_areas"]
        ))

    def test_heading_list_and_coverage_statement_are_unioned(self):
        facts = build_page_facts(
            """
            We serve properties throughout Nassau County and Suffolk County,
            including Hempstead and Levittown.
            Service Area
            Long Island
            Nassau County
            Suffolk County
            Babylon
            Gallery
            """
        )

        self.assertEqual(
            facts["service_areas"],
            [
                "Long Island",
                "Nassau County",
                "Suffolk County",
                "Babylon",
                "Hempstead",
                "Levittown",
            ],
        )

    def test_navigation_label_alone_is_not_a_service_area_fact(self):
        facts = build_page_facts(
            "[Service Areas](https://example.com/service-areas/)"
        )

        self.assertEqual(facts["service_areas"], [])

    def test_two_state_list_is_not_misread_as_city_state_pair(self):
        facts = build_page_facts("Serving New York and New Jersey.")

        self.assertEqual(facts["service_areas"], ["New York", "New Jersey"])

    def test_marketing_audience_is_not_split_into_place_names(self):
        facts = build_page_facts(
            "Serving homeowners and businesses in Manhattan, NY and the "
            "following communities in and around Manhattan and the entire "
            "New York metro area."
        )

        self.assertEqual(facts["service_areas"], [])

    def test_jsonld_and_visible_sources_are_additive(self):
        facts = build_service_area_facts(
            "Serving Broken Arrow and Sapulpa.",
            [{"areaServed": [{"name": "Tulsa"}, "Broken Arrow"]}],
        )

        self.assertEqual(
            facts["service_areas"],
            ["Tulsa", "Broken Arrow", "Sapulpa"],
        )
        broken_arrow = next(
            item for item in facts["observations"]
            if item["value"] == "Broken Arrow"
        )
        self.assertEqual(
            broken_arrow["corroborating_sources"],
            ["page.jsonld.area_served", "page.dom.service_area_statement"],
        )

    def test_geographic_aliases_and_containment_are_semantic_matches(self):
        for page, gbp in (
            (["Long Island"], ["Nassau County"]),
            (["New York City"], ["Brooklyn"]),
            (["Tri-State"], ["New Jersey", "Connecticut"]),
            (["Tri-State"], ["Brooklyn"]),
            (["Greater Tulsa Area"], ["Tulsa"]),
        ):
            with self.subTest(page=page, gbp=gbp):
                condition, match_type, _, pairs = compare_service_area_sets(page, gbp)
                self.assertEqual(condition, "semantic_match")
                self.assertEqual(match_type, "semantic")
                self.assertTrue(pairs)

    def test_unrelated_specific_places_remain_a_material_conflict(self):
        condition, match_type, _, pairs = compare_service_area_sets(
            ["Tulsa"],
            ["Dallas"],
        )

        self.assertEqual(condition, "material_conflict")
        self.assertEqual(match_type, "conflict")
        self.assertEqual(pairs, [])


if __name__ == "__main__":
    unittest.main()
