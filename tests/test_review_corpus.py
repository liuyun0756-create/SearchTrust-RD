import json
import unittest

from app.report_v21.evidence_ledger import build_evidence_ledger, build_layer_evidence
from app.report_v21.review_corpus import build_review_corpus, serialize_review_corpus


class ReviewCorpusTests(unittest.TestCase):
    def test_combines_page_testimonials_and_recent_gbp_reviews(self):
        content = """# Emergency Plumbing
Service information.
## Customer Reviews
They repaired our water heater in Tulsa and explained every step.
## Contact Us
Call today.
"""
        corpus = build_review_corpus(
            content,
            {
                "review_list": [
                    {
                        "author": "A Customer",
                        "rating": 5,
                        "text": "Fast drain cleaning at our Broken Arrow home.",
                    }
                ]
            },
            page_url="https://example.com/service",
            gbp_url="https://maps.example/profile",
        )

        self.assertEqual([item["source"] for item in corpus], ["page", "gbp"])
        self.assertEqual(corpus[0]["id"], "page-review-01")
        self.assertEqual(corpus[1]["id"], "gbp-review-01")
        self.assertEqual(json.loads(serialize_review_corpus(corpus)), corpus)

    def test_empty_corpus_creates_honest_missing_review_evidence(self):
        context = {
            "url": "https://example.com/service",
            "content": "# Emergency Plumbing\nCall today for service.",
            "page_content": "# Emergency Plumbing\nCall today for service.",
            "gbp_data": {},
            "review_corpus": [],
        }
        ledger = build_evidence_ledger(context)
        evidence = build_layer_evidence([37, 38], ledger, context)

        self.assertEqual(len(evidence), 2)
        self.assertEqual(
            [item["source_label"] for item in evidence],
            ["Review service detail", "Review geographic context"],
        )
        for item in evidence:
            self.assertEqual(item["source_type"], "review")
            self.assertEqual(item["comparison_result"], "missing")
            self.assertTrue(item["normalized_value"].startswith("Not found:"))

    def test_same_snapshot_serializes_identically_three_times(self):
        gbp = {"review_list": [{"text": "Great service in Tulsa.", "rating": 5}]}
        snapshots = [
            serialize_review_corpus(build_review_corpus("Page content", gbp))
            for _ in range(3)
        ]
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[1], snapshots[2])


if __name__ == "__main__":
    unittest.main()
