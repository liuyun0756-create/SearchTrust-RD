import unittest
from unittest.mock import AsyncMock, patch

from app.report_v21.page_facts import build_page_facts
from app.tasks import address_ai


MICHIGAN_FOOTER = """
Michigan Plumbing
6204 Lansing Road
Lansing, MI 48917
Phone: 517-322-2994
Email: info@michiganplumbing.com
"""


class AddressAiFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirms_adjacent_source_grounded_address_lines(self):
        initial = build_page_facts(MICHIGAN_FOOTER)
        payload = {
            "addresses": [{
                "candidate_id": 1,
                "is_address": True,
                "full_address": "6204 Lansing Road, Lansing, MI 48917",
                "components": {
                    "street": "6204 Lansing Road",
                    "city": "Lansing",
                    "state": "MI",
                    "postal_code": "48917",
                },
                "evidence_lines": ["6204 Lansing Road", "Lansing, MI 48917"],
            }],
        }

        with patch.object(address_ai, "_configured", return_value=True), patch.object(
            address_ai,
            "_request_confirmation",
            new=AsyncMock(return_value=payload),
        ):
            candidates = await address_ai.confirm_incomplete_address_candidates(
                MICHIGAN_FOOTER,
                initial,
                source_url="https://michiganplumbing.com/plumbing-repairs",
            )

        facts = build_page_facts(
            MICHIGAN_FOOTER,
            additional_address_candidates=candidates,
        )
        self.assertEqual(
            facts["addresses"],
            ["6204 Lansing Road, Lansing, MI 48917"],
        )
        self.assertEqual(
            facts["observations"]["addresses"][0]["source_type"],
            "page.ai.confirmed_address",
        )

    async def test_rejects_model_address_that_adds_a_city_not_in_source(self):
        initial = build_page_facts(MICHIGAN_FOOTER)
        payload = {
            "addresses": [{
                "candidate_id": 1,
                "is_address": True,
                "full_address": "6204 Lansing Road, Grand Rapids, MI 48917",
                "components": {
                    "street": "6204 Lansing Road",
                    "city": "Grand Rapids",
                    "state": "MI",
                    "postal_code": "48917",
                },
                "evidence_lines": ["6204 Lansing Road", "Lansing, MI 48917"],
            }],
        }

        with patch.object(address_ai, "_configured", return_value=True), patch.object(
            address_ai,
            "_request_confirmation",
            new=AsyncMock(return_value=payload),
        ):
            candidates = await address_ai.confirm_incomplete_address_candidates(
                MICHIGAN_FOOTER,
                initial,
            )

        self.assertEqual(candidates, [])

    async def test_provider_failure_keeps_deterministic_result(self):
        initial = build_page_facts(MICHIGAN_FOOTER)

        with patch.object(address_ai, "_configured", return_value=True), patch.object(
            address_ai,
            "_request_confirmation",
            new=AsyncMock(side_effect=TimeoutError("provider timeout")),
        ):
            candidates = await address_ai.confirm_incomplete_address_candidates(
                MICHIGAN_FOOTER,
                initial,
            )

        self.assertEqual(candidates, [])

    async def test_does_not_call_model_without_partial_street_candidate(self):
        initial = build_page_facts("Contact us for service in Lansing, Michigan.")
        request = AsyncMock()

        with patch.object(address_ai, "_configured", return_value=True), patch.object(
            address_ai,
            "_request_confirmation",
            new=request,
        ):
            candidates = await address_ai.confirm_incomplete_address_candidates(
                "Contact us for service in Lansing, Michigan.",
                initial,
            )

        self.assertEqual(candidates, [])
        request.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
