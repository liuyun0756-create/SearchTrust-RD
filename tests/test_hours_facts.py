import unittest

from app.report_v21.hours_facts import (
    build_gbp_hours_facts,
    build_source_facts,
    compare_page_gbp_hours,
    extract_page_hours_facts,
    normalize_hours_text,
)
from app.report_v21.page_facts import build_page_facts
from app.tasks.scraper import _build_gbp_info


RAPID_ROOTER_URL = (
    "https://rapidrooterplumber.com/services/"
    "drain-sewer-services/drain-cleaning/"
)


class HoursFactsTests(unittest.TestCase):
    def test_rapid_rooter_preserves_header_and_footer_semantics(self):
        html = """
        <html>
          <header><div>Mon-Sat : 24-Hours Emergency Service - Call (559) 440-9314</div></header>
          <main><h1>Drain Cleaning</h1></main>
          <footer>
            <div><strong>Mon - Sat:</strong> 12:00 AM - 12:00 PM</div>
            <div><strong>Sun:</strong> Closed</div>
          </footer>
        </html>
        """

        facts = extract_page_hours_facts(html, html, source_url=RAPID_ROOTER_URL)
        observations = facts["observations"]

        self.assertTrue(facts["present"])
        self.assertTrue(any(
            item["source_location"] == "header"
            and item["semantic_role"] == "emergency_availability"
            and "24-Hours Emergency Service" in item["raw_value"]
            for item in observations
        ))
        self.assertTrue(any(
            item["source_location"] == "footer"
            and item["semantic_role"] == "regular_business_hours"
            and "12:00 AM - 12:00 PM" in item["raw_value"]
            for item in observations
        ))
        self.assertFalse(facts["conflicts_by_semantic_role"]["emergency_availability"])

    def test_json_ld_hours_are_saved_as_structured_source(self):
        html = """
        <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "Plumber",
            "name": "Example Plumbing",
            "openingHoursSpecification": [{
              "@type": "OpeningHoursSpecification",
              "dayOfWeek": ["Monday", "Tuesday"],
              "opens": "00:00",
              "closes": "00:00"
            }]
          }
        </script>
        """

        facts = extract_page_hours_facts("Example Plumbing", html, source_url="https://example.com")

        self.assertEqual(facts["observations"][0]["source_location"], "json_ld")
        self.assertEqual(facts["observations"][0]["semantic_role"], "regular_business_hours")

    def test_gbp_summary_and_weekly_hours_are_separate(self):
        provider_result = {
            "title": "Rapid Rooter Drain Master & Plumbing Experts",
            "open_state": "Open 24 hours",
            "operating_hours": {
                "monday": "Open 24 hours",
                "tuesday": "Open 24 hours",
                "wednesday": "Open 24 hours",
                "thursday": "Open 24 hours",
                "friday": "Open 24 hours",
                "saturday": "Open 24 hours",
                "sunday": "Closed",
            },
            "data_id": "rapid-rooter-data-id",
        }

        gbp = _build_gbp_info(provider_result)
        facts = build_gbp_hours_facts(gbp)

        self.assertEqual(gbp["hours_summary"], "Open 24 hours")
        self.assertEqual(gbp["operating_hours_raw"]["sunday"], "Closed")
        self.assertEqual(facts["normalized_schedule"]["monday"]["intervals"], [["00:00", "24:00"]])
        self.assertEqual(facts["normalized_schedule"]["sunday"]["status"], "closed")
        self.assertTrue(facts["complete"])

    def test_source_facts_snapshot_is_safe_and_versioned(self):
        content = "Mon-Sat: 24-Hours Emergency Service\nSun: Closed"
        page_facts = build_page_facts(content, source_url=RAPID_ROOTER_URL)
        source_facts = build_source_facts(
            page_facts,
            {
                "open_state": "Open 24 hours",
                "operating_hours": {"monday": "Open 24 hours", "sunday": "Closed"},
                "data_id": "public-id",
                "api_key": "must-not-be-persisted",
            },
            source_url=RAPID_ROOTER_URL,
            fetched_at="2026-08-12T09:07:00+00:00",
        )

        self.assertEqual(source_facts["schema_version"], "1")
        self.assertNotIn("api_key", repr(source_facts))
        self.assertEqual(
            source_facts["gbp"]["opening_hours"]["fetched_at"],
            "2026-08-12T09:07:00+00:00",
        )

    def test_midnight_to_noon_is_not_normalized_as_24_hours(self):
        normalized = normalize_hours_text("Mon-Sat: 12:00 AM - 12:00 PM")

        self.assertEqual(normalized["monday"]["intervals"], [["00:00", "12:00"]])

    def test_provider_multiple_intervals_inherit_meridiem(self):
        normalized = normalize_hours_text(
            "Monday: 7:30–10:30 AM, 12–10:30 PM"
        )

        self.assertEqual(
            normalized["monday"]["intervals"],
            [["07:30", "10:30"], ["12:00", "22:30"]],
        )

    def test_time_before_weekday_range_is_extracted_as_comparable_hours(self):
        html = """
        <footer>
          <div>Office Hours:</div>
          <div>7:30am - 5:00pm, Mon-Fri</div>
          <div>Same Day Services Available</div>
        </footer>
        """
        page_facts = build_page_facts(
            html,
            structured_content=html,
            source_url="https://lakemihcp.com/plumbing/",
        )
        source_facts = build_source_facts(
            page_facts,
            {
                "operating_hours_raw": {
                    day: "Open 24 hours"
                    for day in (
                        "monday", "tuesday", "wednesday", "thursday",
                        "friday", "saturday", "sunday",
                    )
                },
            },
            source_url="https://lakemihcp.com/plumbing/",
        )

        observation = page_facts["opening_hours"]["observations"][0]
        comparison = compare_page_gbp_hours(source_facts)

        self.assertTrue(page_facts["opening_hours"]["present"])
        self.assertEqual(observation["raw_value"], "7:30am - 5:00pm, Mon-Fri")
        self.assertEqual(
            observation["normalized_schedule"]["monday"]["intervals"],
            [["07:30", "17:00"]],
        )
        self.assertTrue(observation["eligible_for_l3"])
        self.assertEqual(comparison["status"], "mismatch")
        self.assertNotEqual(comparison["status"], "missing")

    def test_strict_comparison_uses_complete_week_and_all_page_sources(self):
        html = """
        <header>Mon-Sat: 24-Hours Emergency Service</header>
        <footer>Mon-Sat: 12:00 AM - 12:00 PM\nSun: Closed</footer>
        """
        page_facts = build_page_facts(html, structured_content=html)
        source_facts = build_source_facts(
            page_facts,
            {
                "hours_summary": "Open 24 hours",
                "operating_hours_raw": {
                    "monday": "Open 24 hours",
                    "tuesday": "Open 24 hours",
                    "wednesday": "Open 24 hours",
                    "thursday": "Open 24 hours",
                    "friday": "Open 24 hours",
                    "saturday": "Open 24 hours",
                    "sunday": "Closed",
                },
            },
            source_url=RAPID_ROOTER_URL,
        )

        comparison = compare_page_gbp_hours(source_facts)

        self.assertEqual(comparison["status"], "match")
        self.assertEqual(comparison["unmatched_days"], [])

    def test_current_hours_summary_alone_is_not_compared(self):
        page_facts = build_page_facts("Open 24 hours")
        source_facts = build_source_facts(
            page_facts,
            {"hours_summary": "Open 24 hours"},
            source_url=RAPID_ROOTER_URL,
        )

        comparison = compare_page_gbp_hours(source_facts)

        self.assertEqual(comparison["status"], "not_checked")

    def test_strict_comparison_reports_days_with_different_values(self):
        page_facts = build_page_facts(
            "Mon-Sat: 12:00 AM - 12:00 PM\nSun: Closed"
        )
        source_facts = build_source_facts(
            page_facts,
            {
                "operating_hours_raw": {
                    day: "Open 24 hours"
                    for day in (
                        "monday", "tuesday", "wednesday", "thursday",
                        "friday", "saturday", "sunday",
                    )
                },
            },
            source_url=RAPID_ROOTER_URL,
        )

        comparison = compare_page_gbp_hours(source_facts)

        self.assertEqual(comparison["status"], "mismatch")
        self.assertIn("monday", comparison["unmatched_days"])
        self.assertIn("sunday", comparison["unmatched_days"])


if __name__ == "__main__":
    unittest.main()
