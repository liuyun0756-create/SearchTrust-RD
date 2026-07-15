import unittest

from app.report_v21.business_presence import (
    bind_business_presence_evidence,
    build_business_presence_audit,
)
from app.tasks.pipeline import _build_dify_gbp_payload


class BusinessPresenceAuditTests(unittest.TestCase):
    def base_context(self):
        return {
            "url": "https://example.com/emergency-plumbing/",
            "page_content": (
                "# Emergency Plumbing | Example Plumbing\n\n"
                "Call (918) 555-0100.\n\n"
                "Serving Tulsa, Broken Arrow and nearby communities.\n"
            ),
            "page_business": {
                "name": "Example Plumbing",
                "phone": "(918) 555-0100",
                "city": "Tulsa",
            },
            "input_gbp_url": "https://maps.example/profile",
            "gbp_url": "https://maps.example/profile",
            "gbp_lookup_attempted": True,
            "gbp_error": None,
            "gbp_data": {
                "name": "Example Plumbing",
                "phone": "+1 918-555-0100",
                "website": "https://www.example.com/",
                "categories": ["Plumber", "Emergency plumber service"],
                "reviews": 2,
                "review_list": [
                    {
                        "author": "A",
                        "rating": 5,
                        "date": "a week ago",
                        "text": "Fast response.",
                        "owner_reply": "Thank you.",
                    },
                    {
                        "author": "B",
                        "rating": 4,
                        "date": "two weeks ago",
                        "text": "Clear communication.",
                        "owner_reply": "",
                    },
                ],
                "review_fetch": {"attempted": True, "error": None},
                "photo_fetch": {"attempted": True, "count": 12, "latest_date": None, "error": None},
                "post_fetch": {"attempted": True, "count": 3, "latest_date": "Jun 2026", "error": None},
            },
        }

    def test_builds_non_scoring_alignment_and_review_summary(self):
        audit = build_business_presence_audit(self.base_context())
        rows = {row["key"]: row for row in audit["gbp_page_alignment"]}

        self.assertEqual(rows["business_name"]["status"], "match")
        self.assertEqual(rows["phone"]["status"], "match")
        self.assertEqual(rows["website"]["status"], "match")
        self.assertEqual(rows["service_area"]["status"], "not_checked")
        self.assertTrue(all(row["included_in_score"] is False for row in rows.values()))
        self.assertEqual(audit["review_audit"]["sample_size"], 2)
        self.assertEqual(audit["review_audit"]["owner_reply_rate"], 0.5)
        self.assertEqual(audit["review_audit"]["rating_distribution"], {"4": 1, "5": 1})

    def test_service_area_explicitly_empty_uses_missing_only_when_applicable(self):
        context = self.base_context()
        context["gbp_data"].update({
            "service_areas": [],
            "service_areas_observed": True,
            "service_area_business": True,
        })

        audit = build_business_presence_audit(context)
        service_area = next(row for row in audit["gbp_page_alignment"] if row["key"] == "service_area")

        self.assertEqual(service_area["status"], "missing")

    def test_storefront_service_area_is_not_applicable(self):
        context = self.base_context()
        context["gbp_data"].update({
            "service_areas": [],
            "service_areas_observed": True,
            "service_area_business": False,
        })

        audit = build_business_presence_audit(context)
        service_area = next(row for row in audit["gbp_page_alignment"] if row["key"] == "service_area")

        self.assertEqual(service_area["status"], "not_applicable")

    def test_review_total_without_records_is_not_reported_as_checked_zero(self):
        context = self.base_context()
        context["gbp_data"].update({
            "reviews": 125,
            "review_list": [],
            "review_fetch": {"attempted": True, "error": "upstream timeout"},
        })

        audit = build_business_presence_audit(context)

        self.assertEqual(audit["review_audit"]["status"], "error")
        self.assertEqual(audit["review_audit"]["sample_size"], 0)

    def test_alignment_evidence_reuses_ids_without_changing_rule_fields(self):
        context = self.base_context()
        audit = build_business_presence_audit(context)
        report = {
            "layers": [{
                "layer_key": "entity_consistency",
                "checked_rule_ids": [26, 27, 28, 29],
                "triggered_rule_ids": [27],
                "evidence_items": [],
            }],
            "key_issues": [{
                "affected_layer": "entity_consistency",
                "evidence_items": [],
            }],
            "primary_blocking_layer": {
                "layer_key": "entity_consistency",
                "evidence_items": [],
            },
        }

        bound = bind_business_presence_evidence(report, audit, context)
        layer = bound["layers"][0]
        issue = bound["key_issues"][0]

        self.assertEqual(layer["checked_rule_ids"], [26, 27, 28, 29])
        self.assertEqual(layer["triggered_rule_ids"], [27])
        self.assertTrue(layer["evidence_items"])
        self.assertEqual(
            [item["id"] for item in layer["evidence_items"]],
            [item["id"] for item in issue["evidence_items"]],
        )

    def test_dify_receives_bounded_legacy_gbp_payload(self):
        payload = _build_dify_gbp_payload({
            "name": "Example Plumbing",
            "review_list": [{"text": str(index)} for index in range(30)],
            "review_fetch": {"attempted": True},
            "photo_fetch": {"attempted": True, "count": 10},
            "post_fetch": {"attempted": True, "count": 2},
        })

        self.assertEqual(len(payload["review_list"]), 10)
        self.assertNotIn("review_fetch", payload)
        self.assertNotIn("photo_fetch", payload)
        self.assertNotIn("post_fetch", payload)


if __name__ == "__main__":
    unittest.main()
