import unittest
from unittest.mock import AsyncMock, patch

from app.tasks import scraper


SHORT_URL = "https://maps.app.goo.gl/spB4reXT8NAMvS8V8"
PAGE_SHORT_URL = "https://goo.gl/maps/BmutfbtV5uvo62km7"
SHARE_URL = "https://share.google/sLiv8JcVCQxW0UVMj"
TRI_CITIES_PLACE_ID = "ChIJgx6lgF55mFQRq17Gwkb_p0Y"
TRI_CITIES_DIRECTIONS_URL = (
    "https://www.google.com/maps/dir/?api=1&"
    "destination=6250+W+Clearwater+Ave%2C+Ste+101%2C+Kennewick%2C+WA+99336&"
    f"destination_place_id={TRI_CITIES_PLACE_ID}"
)
TRI_CITIES_DATA_ID = "0x5498795e80a51e83:0x46a7ff46c2c65eab"
TULSA_URL = (
    "https://www.google.com/maps/place/Spot+On+Plumbing+of+Tulsa+Plumbers/"
    "data=!4m6!3m5!1s0x87b68befcc42b925:0x20f8d8fccd659226"
)


class _FakeResponse:
    def __init__(self, *, url: str, payload=None, text=""):
        self.url = url
        self._payload = payload or {}
        self.text = text

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
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class GbpLookupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scraper._GBP_SHORT_URL_CACHE.clear()

    def test_extracts_google_maps_short_url_from_page_content(self):
        content = (
            "Quick Contact\n"
            f"[1911 West Reno Street]({PAGE_SHORT_URL})\n"
            "[Google](https://example.com)"
        )

        self.assertEqual(scraper.extract_maps_url_from_content(content), PAGE_SHORT_URL)

    def test_prefers_exact_google_maps_long_url_over_short_url(self):
        content = f"[Address]({PAGE_SHORT_URL})\n[Google Business Profile]({TULSA_URL})"

        self.assertEqual(scraper.extract_maps_url_from_content(content), TULSA_URL)

    def test_extracts_google_directions_place_id_before_share_link(self):
        content = (
            f"[Google Business Profile]({SHARE_URL})\n"
            f"[Get Directions]({TRI_CITIES_DIRECTIONS_URL})"
        )

        self.assertEqual(
            scraper.extract_maps_url_from_content(content),
            TRI_CITIES_DIRECTIONS_URL,
        )
        self.assertEqual(
            scraper._extract_google_place_id(TRI_CITIES_DIRECTIONS_URL),
            TRI_CITIES_PLACE_ID,
        )

    def test_extracts_share_google_link_when_no_exact_maps_link_exists(self):
        self.assertEqual(
            scraper.extract_maps_url_from_content(
                f"[Google Business Profile]({SHARE_URL})"
            ),
            SHARE_URL,
        )

    def test_derives_multi_location_branch_root_without_affecting_generic_paths(self):
        page_url = "https://www.1tomplumber.com/tri-cities-wa/services/plumbing/"
        content = "[Tri-Cities](https://www.1tomplumber.com/tri-cities-wa/)"

        self.assertEqual(
            scraper._derive_branch_root_url(page_url, content),
            "https://www.1tomplumber.com/tri-cities-wa/",
        )
        self.assertIsNone(
            scraper._derive_branch_root_url(
                "https://single-store.example/services/plumbing/",
                "[Services](/services/)",
            )
        )

    async def test_resolves_maps_short_url_to_data_id_url(self):
        client = _FakeClient([_FakeResponse(url=TULSA_URL)])
        with patch("app.tasks.scraper.httpx.AsyncClient", return_value=client):
            resolved = await scraper._resolve_gbp_url(SHORT_URL)

        self.assertEqual(resolved, TULSA_URL)
        self.assertEqual(
            scraper._extract_data_id_from_gbp_url(resolved),
            "0x87b68befcc42b925:0x20f8d8fccd659226",
        )

    async def test_resolves_directions_place_id_from_google_response_body(self):
        client = _FakeClient([
            _FakeResponse(
                url=TRI_CITIES_DIRECTIONS_URL,
                text=f'<script>window.APP_INITIALIZATION_STATE="{TRI_CITIES_DATA_ID}"</script>',
            )
        ])
        with patch("app.tasks.scraper.httpx.AsyncClient", return_value=client):
            resolved = await scraper._resolve_gbp_url(TRI_CITIES_DIRECTIONS_URL)

        self.assertEqual(
            scraper._extract_data_id_from_gbp_url(resolved),
            TRI_CITIES_DATA_ID,
        )

    async def test_scrape_uses_branch_root_place_id_for_multi_location_page(self):
        page_url = "https://www.1tomplumber.com/tri-cities-wa/services/plumbing/"
        branch_url = "https://www.1tomplumber.com/tri-cities-wa/"
        main_content = (
            f"[Tri-Cities]({branch_url})\n"
            f"[Google Business Profile]({SHARE_URL})\n"
            "# Plumbing Services in Tri-Cities"
        )
        branch_content = f"[Get Directions]({TRI_CITIES_DIRECTIONS_URL})"
        fetch_page = AsyncMock(side_effect=[
            scraper.ScrapeResult(
                content=main_content,
                source=scraper.ScraperSource.JINA,
                elapsed=0.1,
                content_length=len(main_content),
            ),
            scraper.ScrapeResult(
                content=branch_content,
                source=scraper.ScraperSource.JINA,
                elapsed=0.1,
                content_length=len(branch_content),
            ),
        ])
        fetch_gbp = AsyncMock(return_value={"name": "1-Tom-Plumber Tri-Cities"})

        with patch.object(scraper.settings, "FIRECRAWL_API_KEY", ""), patch(
            "app.tasks.scraper.fetch_page_content", fetch_page
        ), patch(
            "app.tasks.scraper.discover_sub_page_urls", return_value=[]
        ), patch(
            "app.tasks.scraper.fetch_gbp_data", fetch_gbp
        ):
            result = await scraper.scrape(page_url)

        self.assertEqual(result["gbp_url"], TRI_CITIES_DIRECTIONS_URL)
        self.assertEqual(result["gbp"]["name"], "1-Tom-Plumber Tri-Cities")
        self.assertEqual(fetch_page.await_args_list[1].args[0], branch_url)
        self.assertEqual(fetch_gbp.await_args.kwargs["gbp_url"], TRI_CITIES_DIRECTIONS_URL)

    async def test_short_url_resolution_retries_and_caches_success(self):
        client = _FakeClient([
            TimeoutError("temporary redirect timeout"),
            _FakeResponse(url=TULSA_URL),
        ])
        sleep = AsyncMock()
        with patch("app.tasks.scraper.httpx.AsyncClient", return_value=client), patch(
            "app.tasks.scraper.asyncio.sleep", sleep
        ):
            first = await scraper._resolve_gbp_url(SHORT_URL)
            second = await scraper._resolve_gbp_url(SHORT_URL)

        self.assertEqual(first, TULSA_URL)
        self.assertEqual(second, TULSA_URL)
        self.assertEqual(len(client.requests), 2)
        sleep.assert_awaited_once_with(1)

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

    async def test_exact_lookup_retries_empty_results_without_cache(self):
        place = {
            "title": "Spot On Plumbing of Tulsa Plumbers",
            "phone": "(918) 844-7961",
            "address": "1911 W Reno St, Broken Arrow, OK 74012",
            "website": "https://spotonplumbing.com/",
        }
        client = _FakeClient([
            _FakeResponse(url="https://serpapi.example/search", payload={}),
            _FakeResponse(url="https://serpapi.example/search", payload={}),
            _FakeResponse(url="https://serpapi.example/search", payload={"place_results": place}),
        ])
        diagnostic = {}
        with patch.object(scraper.settings, "SERPAPI_KEY", "test-key"), patch(
            "app.tasks.scraper._resolve_gbp_url", new=AsyncMock(return_value=TULSA_URL)
        ), patch("app.tasks.scraper.httpx.AsyncClient", return_value=client), patch(
            "app.tasks.scraper._enrich_gbp_info", new=AsyncMock(side_effect=lambda value: value)
        ), patch("app.tasks.scraper.asyncio.sleep", new=AsyncMock()):
            result = await scraper.fetch_gbp_data(
                business_name="Spot On Plumbing",
                city="Tulsa",
                website_url="https://spotonplumbing.com/emergency-services/",
                gbp_url=SHORT_URL,
                diagnostic=diagnostic,
            )

        self.assertEqual(result["name"], "Spot On Plumbing of Tulsa Plumbers")
        self.assertEqual(diagnostic["code"], "exact_cid_match")
        self.assertNotIn("no_cache", client.requests[0][1]["params"])
        self.assertEqual(client.requests[1][1]["params"]["no_cache"], "true")
        self.assertEqual(client.requests[2][1]["params"]["no_cache"], "true")

    async def test_unresolved_supplied_maps_url_uses_strict_domain_fallback(self):
        place = {
            "title": "Spot On Plumbing of Tulsa Plumbers",
            "phone": "(918) 844-7961",
            "address": "1911 W Reno St, Broken Arrow, OK 74012",
            "website": "https://spotonplumbing.com/",
        }
        client = _FakeClient([
            _FakeResponse(url="https://serpapi.example/search", payload={"place_results": place})
        ])
        diagnostic = {}
        with patch.object(scraper.settings, "SERPAPI_KEY", "test-key"), patch(
            "app.tasks.scraper._resolve_gbp_url", new=AsyncMock(return_value=SHORT_URL)
        ), patch("app.tasks.scraper.httpx.AsyncClient", return_value=client), patch(
            "app.tasks.scraper._enrich_gbp_info", new=AsyncMock(side_effect=lambda value: value)
        ):
            result = await scraper.fetch_gbp_data(
                business_name="Spot On Plumbing",
                city="Tulsa",
                website_url="https://spotonplumbing.com/emergency-services/",
                gbp_url=SHORT_URL,
                diagnostic=diagnostic,
            )

        self.assertEqual(result["name"], "Spot On Plumbing of Tulsa Plumbers")
        self.assertEqual(diagnostic["code"], "strict_domain_fallback_match")
        self.assertTrue(client.requests[0][1]["params"]["q"].endswith("spotonplumbing.com"))

    async def test_empty_exact_lookup_uses_strict_domain_fallback(self):
        place = {
            "title": "Spot On Plumbing of Tulsa Plumbers",
            "phone": "(918) 844-7961",
            "address": "1911 W Reno St, Broken Arrow, OK 74012",
            "website": "https://spotonplumbing.com/",
        }
        empty = _FakeResponse(url="https://serpapi.example/search", payload={})
        client = _FakeClient([
            empty,
            empty,
            empty,
            _FakeResponse(url="https://serpapi.example/search", payload={"place_results": place}),
        ])
        diagnostic = {}
        with patch.object(scraper.settings, "SERPAPI_KEY", "test-key"), patch(
            "app.tasks.scraper._resolve_gbp_url", new=AsyncMock(return_value=TULSA_URL)
        ), patch("app.tasks.scraper.httpx.AsyncClient", return_value=client), patch(
            "app.tasks.scraper._enrich_gbp_info", new=AsyncMock(side_effect=lambda value: value)
        ), patch("app.tasks.scraper.asyncio.sleep", new=AsyncMock()):
            result = await scraper.fetch_gbp_data(
                business_name="Spot On Plumbing",
                city="Tulsa",
                website_url="https://spotonplumbing.com/emergency-services/",
                gbp_url=SHORT_URL,
                diagnostic=diagnostic,
            )

        self.assertEqual(result["name"], "Spot On Plumbing of Tulsa Plumbers")
        self.assertEqual(diagnostic["code"], "strict_domain_fallback_match")
        self.assertEqual(len(client.requests), 4)

    async def test_domain_search_retries_empty_result_and_recovers(self):
        place = {
            "title": "Spot On Plumbing of Tulsa Plumbers",
            "phone": "(918) 844-7961",
            "address": "1911 W Reno St, Broken Arrow, OK 74012",
            "website": "https://spotonplumbing.com/",
        }
        client = _FakeClient([
            _FakeResponse(url="https://serpapi.example/search", payload={}),
            _FakeResponse(url="https://serpapi.example/search", payload={"place_results": place}),
        ])
        diagnostic = {}
        with patch.object(scraper.settings, "SERPAPI_KEY", "test-key"), patch(
            "app.tasks.scraper.httpx.AsyncClient", return_value=client
        ), patch(
            "app.tasks.scraper._enrich_gbp_info", new=AsyncMock(side_effect=lambda value: value)
        ), patch("app.tasks.scraper.asyncio.sleep", new=AsyncMock()):
            result = await scraper.fetch_gbp_data(
                business_name="Spot On Plumbing",
                city="Tulsa",
                website_url="https://spotonplumbing.com/emergency-services/",
                diagnostic=diagnostic,
            )

        self.assertEqual(result["name"], "Spot On Plumbing of Tulsa Plumbers")
        self.assertEqual(diagnostic["code"], "search_match")
        self.assertEqual(len(client.requests), 2)
        self.assertNotIn("no_cache", client.requests[0][1]["params"])
        self.assertEqual(client.requests[1][1]["params"]["no_cache"], "true")

    async def test_multi_location_domain_search_requires_page_location_match(self):
        wrong_place = {
            "title": "1-Tom-Plumber Tulsa",
            "address": "9525 E 51st St Ste G, Tulsa, OK 74145",
            "website": "https://www.1tomplumber.com/",
        }
        correct_place = {
            "title": "1-Tom-Plumber Tri-Cities",
            "address": "Kennewick, WA 99336",
            "website": "https://www.1tomplumber.com/tri-cities-wa/",
        }
        client = _FakeClient([
            _FakeResponse(
                url="https://serpapi.example/search",
                payload={"local_results": [wrong_place, correct_place]},
            )
        ])
        diagnostic = {}
        with patch.object(scraper.settings, "SERPAPI_KEY", "test-key"), patch(
            "app.tasks.scraper.httpx.AsyncClient", return_value=client
        ), patch(
            "app.tasks.scraper._enrich_gbp_info",
            new=AsyncMock(side_effect=lambda value: value),
        ):
            result = await scraper.fetch_gbp_data(
                business_name="1-Tom-Plumber",
                city="Richland",
                website_url=(
                    "https://www.1tomplumber.com/tri-cities-wa/services/plumbing/"
                ),
                location_hints=["Kennewick", "Pasco", "West Richland"],
                diagnostic=diagnostic,
            )

        self.assertEqual(result["name"], "1-Tom-Plumber Tri-Cities")
        self.assertEqual(diagnostic["code"], "search_match")
        self.assertIn("Richland", client.requests[0][1]["params"]["q"])

    async def test_domain_search_requires_three_no_matches_before_not_found(self):
        empty = _FakeResponse(url="https://serpapi.example/search", payload={})
        client = _FakeClient([empty, empty, empty])
        diagnostic = {}
        with patch.object(scraper.settings, "SERPAPI_KEY", "test-key"), patch(
            "app.tasks.scraper.httpx.AsyncClient", return_value=client
        ), patch(
            "app.tasks.scraper._enrich_gbp_info", new=AsyncMock()
        ), patch("app.tasks.scraper.asyncio.sleep", new=AsyncMock()):
            result = await scraper.fetch_gbp_data(
                business_name="Spot On Plumbing",
                city="Tulsa",
                website_url="https://spotonplumbing.com/emergency-services/",
                diagnostic=diagnostic,
            )

        self.assertEqual(result, {})
        self.assertEqual(len(client.requests), 3)
        self.assertEqual(diagnostic["status"], "not_found")
        self.assertEqual(diagnostic["code"], "search_no_match")

    async def test_strict_domain_fallback_rejects_wrong_naples_business(self):
        wrong_place = {
            "title": "Spot-On Plumbing Service, Inc",
            "phone": "(239) 777-3322",
            "address": "Naples, FL",
            "website": "https://spotonplumbingservice.com/",
        }
        client = _FakeClient([
            _FakeResponse(url="https://serpapi.example/search", payload={"place_results": wrong_place}),
            _FakeResponse(url="https://serpapi.example/search", payload={"place_results": wrong_place}),
            _FakeResponse(url="https://serpapi.example/search", payload={"place_results": wrong_place}),
        ])
        diagnostic = {}
        with patch.object(scraper.settings, "SERPAPI_KEY", "test-key"), patch(
            "app.tasks.scraper._resolve_gbp_url", new=AsyncMock(return_value=SHORT_URL)
        ), patch("app.tasks.scraper.httpx.AsyncClient", return_value=client), patch(
            "app.tasks.scraper._enrich_gbp_info", new=AsyncMock()
        ):
            result = await scraper.fetch_gbp_data(
                business_name="Spot On Plumbing",
                city="Tulsa",
                website_url="https://spotonplumbing.com/emergency-services/",
                gbp_url=SHORT_URL,
                diagnostic=diagnostic,
            )

        self.assertEqual(result, {})
        self.assertEqual(diagnostic["code"], "strict_fallback_no_match")

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
            ),
            _FakeResponse(
                url="https://serpapi.example/search",
                payload={"place_results": wrong_place},
            ),
            _FakeResponse(
                url="https://serpapi.example/search",
                payload={"place_results": wrong_place},
            ),
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
