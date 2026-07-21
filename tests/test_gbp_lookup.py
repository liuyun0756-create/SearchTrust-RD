import unittest
from unittest.mock import AsyncMock, patch

from app.tasks import scraper


SHORT_URL = "https://maps.app.goo.gl/spB4reXT8NAMvS8V8"
TULSA_URL = (
    "https://www.google.com/maps/place/Spot+On+Plumbing+of+Tulsa+Plumbers/"
    "data=!4m6!3m5!1s0x87b68befcc42b925:0x20f8d8fccd659226"
)


class _FakeResponse:
    def __init__(self, *, url: str, payload=None):
        self.url = url
        self._payload = payload or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


class GbpLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_maps_short_url_to_data_id_url(self):
        client = _FakeClient([_FakeResponse(url=TULSA_URL)])
        with patch("app.tasks.scraper.httpx.AsyncClient", return_value=client):
            resolved = await scraper._resolve_gbp_url(SHORT_URL)

        self.assertEqual(resolved, TULSA_URL)
        self.assertEqual(
            scraper._extract_data_id_from_gbp_url(resolved),
            "0x87b68befcc42b925:0x20f8d8fccd659226",
        )

    def test_extracts_percent_encoded_data_id(self):
        encoded = TULSA_URL.replace(":0x20f8", "%3A0x20f8")
        self.assertEqual(
            scraper._extract_data_id_from_gbp_url(encoded),
            "0x87b68befcc42b925:0x20f8d8fccd659226",
        )

    def test_converts_maps_data_id_to_decimal_cid(self):
        self.assertEqual(
            scraper._data_cid_from_data_id("0x87b68befcc42b925:0x20f8d8fccd659226"),
            "2375887383727280678",
        )

    async def test_short_url_uses_exact_place_lookup(self):
        place = {
            "title": "Spot On Plumbing of Tulsa Plumbers",
            "phone": "(918) 844-7961",
            "address": "1911 W Reno St, Broken Arrow, OK 74012",
            "website": "https://spotonplumbing.com/",
        }
        client = _FakeClient([
            _FakeResponse(url="https://serpapi.example/search", payload={"place_results": place})
        ])

        with patch.object(scraper.settings, "SERPAPI_KEY", "test-key"), patch(
            "app.tasks.scraper._resolve_gbp_url", new=AsyncMock(return_value=TULSA_URL)
        ), patch("app.tasks.scraper.httpx.AsyncClient", return_value=client), patch(
            "app.tasks.scraper._enrich_gbp_info", new=AsyncMock(side_effect=lambda value: value)
        ):
            result = await scraper.fetch_gbp_data(
                business_name="Spot On Plumbing",
                city="Tulsa",
                website_url="https://spotonplumbing.com/emergency-services/",
                gbp_url=SHORT_URL,
            )

        self.assertEqual(result["name"], "Spot On Plumbing of Tulsa Plumbers")
        self.assertEqual(result["data_id"], "0x87b68befcc42b925:0x20f8d8fccd659226")
        params = client.requests[0][1]["params"]
        self.assertNotIn("type", params)
        self.assertEqual(params["data_cid"], "2375887383727280678")

    async def test_unresolved_supplied_maps_url_does_not_fallback_to_name_search(self):
        client_factory = AsyncMock()
        with patch.object(scraper.settings, "SERPAPI_KEY", "test-key"), patch(
            "app.tasks.scraper._resolve_gbp_url", new=AsyncMock(return_value=SHORT_URL)
        ), patch("app.tasks.scraper.httpx.AsyncClient", client_factory):
            result = await scraper.fetch_gbp_data(
                business_name="Spot On Plumbing",
                city="Tulsa",
                website_url="https://spotonplumbing.com/emergency-services/",
                gbp_url=SHORT_URL,
            )

        self.assertEqual(result, {})
        client_factory.assert_not_called()

    async def test_rejects_singular_place_result_without_domain_or_city_match(self):
        wrong_place = {
            "title": "Spot-On Plumbing Service, Inc",
            "phone": "(239) 777-3322",
            "address": "Naples, FL",
            "website": "",
        }
        client = _FakeClient([
            _FakeResponse(
                url="https://serpapi.example/search",
                payload={"place_results": wrong_place},
            )
        ])
        enrich = AsyncMock()

        with patch.object(scraper.settings, "SERPAPI_KEY", "test-key"), patch(
            "app.tasks.scraper.httpx.AsyncClient", return_value=client
        ), patch("app.tasks.scraper._enrich_gbp_info", enrich):
            result = await scraper.fetch_gbp_data(
                business_name="Spot On Plumbing",
                city="Tulsa",
                website_url="https://spotonplumbing.com/emergency-services/",
            )

        self.assertEqual(result, {})
        enrich.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
