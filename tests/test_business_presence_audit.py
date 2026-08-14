import unittest

from app.report_v21.business_presence import (
    bind_business_presence_evidence,
    build_business_presence_audit,
)
from app.report_v21.gbp_rule_evaluator import evaluate_gbp_rules
from app.report_v21.hours_facts import build_source_facts
from app.report_v21.page_facts import build_page_facts
from app.tasks.pipeline import _build_dify_gbp_payload
from app.tasks.scraper import extract_business_info


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
        self.assertIn("comparable", rows["service_area"]["explanation"])
        alignment_scope = next(
            item for item in audit["audit_scope"] if item["key"] == "gbp_page_alignment"
        )
        self.assertEqual(alignment_scope["status"], "checked")
        self.assertTrue(all(row["included_in_score"] is False for row in rows.values()))
        self.assertEqual(audit["review_audit"]["sample_size"], 2)
        self.assertEqual(audit["review_audit"]["owner_reply_rate"], 0.5)
        self.assertEqual(audit["review_audit"]["rating_distribution"], {"4": 1, "5": 1})
        self.assertEqual(audit["review_audit"]["unanswered_count"], 1)
        self.assertEqual(audit["review_audit"]["low_rating_count"], 0)
        self.assertEqual(audit["review_audit"]["low_rating_unanswered_count"], 0)
        self.assertEqual(audit["review_audit"]["detailed_positive_count"], 0)
        self.assertTrue(audit["proposal_actions"])
        self.assertEqual(audit["proposal_status"], "needs_attention")
        self.assertFalse(any(action["business_area"] == "profile_activity" for action in audit["proposal_actions"]))

    def test_service_area_explicitly_empty_is_not_a_semantic_conflict(self):
        context = self.base_context()
        context["gbp_data"].update({
            "service_areas": [],
            "service_areas_observed": True,
            "service_area_business": True,
        })

        audit = build_business_presence_audit(context)
        service_area = next(row for row in audit["gbp_page_alignment"] if row["key"] == "service_area")

        self.assertEqual(service_area["status"], "not_checked")

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

    def test_low_rating_unanswered_review_becomes_high_priority_task(self):
        context = self.base_context()
        context["gbp_data"]["review_list"] = [{
            "author": "Concerned customer",
            "rating": 2,
            "date": "yesterday",
            "text": "The appointment window was missed and I did not receive an update.",
            "owner_reply": "",
        }]

        audit = build_business_presence_audit(context)
        review = audit["review_audit"]
        action = next(item for item in audit["proposal_actions"] if item["id"] == "bp-action-low-rating-replies")

        self.assertEqual(review["low_rating_count"], 1)
        self.assertEqual(review["low_rating_unanswered_count"], 1)
        self.assertEqual(action["priority"], "high")

    def test_detailed_positive_review_is_counted_without_semantic_inference(self):
        context = self.base_context()
        context["gbp_data"]["review_list"] = [{
            "author": "Customer",
            "rating": 5,
            "date": "last week",
            "text": "The technician arrived on time, explained the repair clearly, protected the work area, and confirmed everything was operating before leaving.",
            "owner_reply": "Thank you.",
        }]

        audit = build_business_presence_audit(context)

        self.assertEqual(audit["review_audit"]["detailed_positive_count"], 1)
        action = next(
            item for item in audit["proposal_actions"]
            if item["id"] == "bp-action-proof-candidates"
        )
        self.assertEqual(action["title"], "Turn detailed reviews into approved customer proof")
        self.assertIn("service pages, case examples and client proposals", action["rationale"])
        self.assertTrue(any("Recommend where each approved quote should appear" in item for item in action["recommended_scope"]))

    def test_review_backlog_task_explains_the_agency_deliverable(self):
        audit = build_business_presence_audit(self.base_context())
        action = next(
            item for item in audit["proposal_actions"]
            if item["id"] == "bp-action-review-backlog"
        )

        self.assertEqual(action["title"], "Complete replies for the remaining recent reviews")
        self.assertIn("consistent review-response process", action["rationale"])
        self.assertTrue(any("approval-ready list" in item for item in action["recommended_scope"]))

    def test_explicit_zero_activity_creates_tasks_but_missing_dates_do_not(self):
        context = self.base_context()
        context["gbp_data"]["photo_fetch"] = {"attempted": True, "count": 0, "latest_date": None, "error": None}
        context["gbp_data"]["post_fetch"] = {"attempted": True, "count": 0, "latest_date": None, "error": None}

        audit = build_business_presence_audit(context)
        ids = {item["id"] for item in audit["proposal_actions"]}

        self.assertIn("bp-action-add-photos", ids)
        self.assertIn("bp-action-add-posts", ids)
        self.assertFalse(any("inactive" in item["title"].lower() for item in audit["proposal_actions"]))

    def test_unavailable_gbp_never_creates_comparison_or_profile_tasks(self):
        context = self.base_context()
        context.update({"input_gbp_url": None, "gbp_url": None, "gbp_lookup_attempted": False, "gbp_data": {}})

        audit = build_business_presence_audit(context)

        self.assertEqual(audit["proposal_status"], "limited")
        self.assertFalse(audit["proposal_actions"])
        self.assertTrue(all(row["status"] == "not_checked" for row in audit["gbp_page_alignment"]))

    def test_fully_aligned_checked_data_does_not_create_false_tasks(self):
        context = self.base_context()
        context["page_content"] = (
            "# Plumber\n\nExample Plumbing\n123 Main Street, Tulsa, OK 74101\n"
            "(918) 555-0100\nOpen 24 hours\nServing Tulsa\n"
        )
        context["gbp_data"].update({
            "address": "123 Main Street, Tulsa, OK 74101",
            "hours": "Open 24 hours",
            "service_areas": ["Tulsa"],
            "service_areas_observed": True,
            "service_area_business": True,
            "categories": ["Plumber"],
            "review_list": [{
                "author": "Customer",
                "rating": 5,
                "date": "today",
                "text": "Great work.",
                "owner_reply": "Thank you.",
            }],
        })

        audit = build_business_presence_audit(context)

        self.assertFalse(any(item["business_area"] == "identity_alignment" for item in audit["proposal_actions"]))
        self.assertFalse(any(item["business_area"] == "profile_activity" for item in audit["proposal_actions"]))
        self.assertFalse(audit["proposal_actions"])

    def test_navigation_links_are_not_treated_as_hours_or_service_area_values(self):
        context = self.base_context()
        context["page_content"] = (
            "[Testimonials](https://example.com/testimonials/)\n"
            "[Service Areas](https://example.com/service-areas/)\n"
        )
        context["page_business"] = {"phone": "+19185550100)\n\n-"}
        context["gbp_data"].update({
            "hours": "Open 24 hours",
            "service_areas": [],
            "service_areas_observed": False,
        })

        audit = build_business_presence_audit(context)
        rows = {row["key"]: row for row in audit["gbp_page_alignment"]}

        self.assertEqual(rows["phone"]["page_value"], "+19185550100")
        self.assertIsNone(rows["opening_hours"]["page_value"])
        self.assertEqual(rows["opening_hours"]["status"], "not_checked")
        self.assertIsNone(rows["service_area"]["page_value"])
        self.assertEqual(rows["service_area"]["status"], "not_checked")

    def test_24_7_page_claim_matches_daily_open_24_hours_profile(self):
        context = self.base_context()
        context["page_content"] = "## Why Choose Example Plumbing's 24/7 Plumbers\n"
        context["gbp_data"]["hours"] = [
            {day: "Open 24 hours"}
            for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        ]

        audit = build_business_presence_audit(context)
        hours = next(row for row in audit["gbp_page_alignment"] if row["key"] == "opening_hours")

        self.assertEqual(hours["status"], "match")

    def test_hours_display_uses_all_page_sources_and_complete_gbp_week(self):
        context = self.base_context()
        context["page_content"] = (
            "Mon-Sat: 24-Hours Emergency Service\n"
            "Mon-Sat: 12:00 AM - 12:00 PM\nSun: Closed"
        )
        context["gbp_data"].update({
            "hours": "Open 24 hours",
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
        })
        page_facts = build_page_facts(context["page_content"], source_url=context["url"])
        context["page_facts"] = page_facts
        context["source_facts"] = build_source_facts(
            page_facts,
            context["gbp_data"],
            source_url=context["url"],
        )

        audit = build_business_presence_audit(context)
        hours = next(row for row in audit["gbp_page_alignment"] if row["key"] == "opening_hours")

        self.assertIn("Emergency Availability", hours["page_value"])
        self.assertIn("Regular Business Hours", hours["page_value"])
        self.assertIn("Monday: Open 24 hours", hours["gbp_value"])
        self.assertIn("Sunday: Closed", hours["gbp_value"])
        self.assertEqual(hours["status"], "match")
        self.assertIn("exactly equal", hours["explanation"])

    def test_business_name_supports_curly_possessive_headings(self):
        info = extract_business_info("## WHY CHOOSE SPOT ON PLUMBING’S 24/7 PLUMBERS\n")

        self.assertEqual(info["name"], "SPOT ON PLUMBING")

    def test_alignment_evidence_reuses_ids_without_changing_rule_fields(self):
        context = self.base_context()
        context["page_content"] += "1911 West Reno Street, Broken Arrow, OK 74012\n"
        context["gbp_data"]["address"] = "999 W Reno St, Broken Arrow, OK 74012"
        audit = build_business_presence_audit(context)
        report = {
            "overall_status": {"label": "Medium", "level": "medium", "explanation": "Fixed score."},
            "ranking_potential": {"label": "Competitive", "level": "competitive", "explanation": "Fixed score."},
            "risk_level": {"label": "Medium", "level": "medium", "explanation": "Fixed score."},
            "layers": [{
                "layer_key": "entity_consistency",
                "status": "medium",
                "checked_rule_ids": [26, 27, 28, 29],
                "triggered_rule_ids": [27],
                "evidence_items": [],
            }],
            "key_issues": [{
                "affected_layer": "entity_consistency",
                "related_rule_ids": [27],
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
        self.assertEqual(layer["status"], "medium")
        self.assertEqual(bound["overall_status"], report["overall_status"])
        self.assertEqual(bound["ranking_potential"], report["ranking_potential"])
        self.assertEqual(bound["risk_level"], report["risk_level"])
        self.assertTrue(layer["evidence_items"])
        self.assertIn("bp-address", [item["id"] for item in layer["evidence_items"]])
        self.assertEqual([item["id"] for item in issue["evidence_items"]], ["bp-address"])

    def test_modern_l3_and_alignment_share_the_same_four_results_and_values(self):
        context = self.base_context()
        context["page_facts"] = {
            "business_names": ["Express 24 Hr Plumbing & Drain LLC."],
            "addresses": [],
            "phones": ["(509) 940-7811", "202.787-.4145"],
            "service_areas": ["Tri Cities Washington", "Our Service Area"],
        }
        context["gbp_data"].update({
            "name": "Express 24 Hr Plumbing & Drain, LLC",
            "address": "6503 W Okanogan Ave Ste f, Kennewick, WA 99336",
            "phone": "(509) 940-7811",
            "service_areas": ["Tri Cities Washington"],
        })
        _, _, findings = evaluate_gbp_rules(context)
        context["backend_gbp_findings"] = findings

        audit = build_business_presence_audit(context)
        rows = {row["key"]: row for row in audit["gbp_page_alignment"]}
        self.assertEqual(
            {key: rows[key]["status"] for key in ("business_name", "address", "phone", "service_area")},
            {
                "business_name": "match",
                "address": "not_checked",
                # The malformed extra candidate is now rejected before L3;
                # the validated primary page number equals the GBP number.
                "phone": "match",
                "service_area": "partial",
            },
        )
        for rule_id, key in ((26, "business_name"), (27, "address"), (28, "phone"), (29, "service_area")):
            self.assertEqual(rows[key]["page_value"], ", ".join(findings[f"rule_{rule_id}"]["page_values"]) or None)
            self.assertEqual(rows[key]["gbp_value"], ", ".join(findings[f"rule_{rule_id}"]["gbp_values"]) or None)

        report = {
            "layers": [{
                "layer_key": "entity_consistency",
                "triggered_rule_ids": [],
                "evidence_items": [{"id": "workflow-contradiction"}],
            }],
            "key_issues": [],
            "primary_blocking_layer": None,
        }
        bound = bind_business_presence_evidence(report, audit, context)
        evidence = bound["layers"][0]["evidence_items"]
        self.assertEqual(evidence, [])
        self.assertTrue(all(not finding["triggered"] for finding in findings.values()))
        self.assertFalse(any(
            action["business_area"] == "identity_alignment"
            for action in audit["proposal_actions"]
        ))

    def test_missing_page_values_remain_l2_coverage_not_l3_conflicts(self):
        context = self.base_context()
        context["page_content"] = "Call (918) 555-0100 for emergency plumbing service."
        context["page_business"] = {"phone": "(918) 555-0100"}
        context["gbp_data"].update({
            "name": "Example Plumbing",
            "address": "123 Main Street, Tulsa, OK 74101",
            "hours": "Open 24 hours",
        })
        audit = build_business_presence_audit(context)
        report = {
            "layers": [{
                "layer_key": "entity_consistency",
                "evidence_items": [],
            }],
            "key_issues": [],
        }

        bound = bind_business_presence_evidence(report, audit, context)
        rows = {row["key"]: row for row in audit["gbp_page_alignment"]}
        self.assertEqual(rows["business_name"]["status"], "not_checked")
        self.assertEqual(rows["address"]["status"], "not_checked")
        evidence_ids = {
            item["id"]
            for item in bound["layers"][0]["evidence_items"]
        }
        self.assertNotIn("bp-business_name", evidence_ids)
        self.assertNotIn("bp-address", evidence_ids)
        self.assertNotIn("bp-opening_hours", evidence_ids)

    def test_dify_gbp_payload_excludes_review_and_backend_audit_details(self):
        payload = _build_dify_gbp_payload({
            "name": "Example Plumbing",
            "review_list": [{"text": str(index)} for index in range(30)],
            "review_fetch": {"attempted": True},
            "photo_fetch": {"attempted": True, "count": 10},
            "post_fetch": {"attempted": True, "count": 2},
        })

        self.assertEqual(payload["name"], "Example Plumbing")
        self.assertNotIn("review_list", payload)
        self.assertNotIn("review_fetch", payload)
        self.assertNotIn("photo_fetch", payload)
        self.assertNotIn("post_fetch", payload)


if __name__ == "__main__":
    unittest.main()
