import unittest
from unittest.mock import AsyncMock, patch

from app.tasks import scraper


SHORT_URL = "https://maps.app.goo.gl/spB4reXT8NAMvS8V8"
PAGE_SHORT_URL = "https://goo.gl/maps/BmutfbtV5uvo62km7"
SHARE_URL = "https://share.google/sLiv8JcVCQxW0UVMj"
REVIEW_PLACE_ID = "ChIJTdp_-Yp4mFQRkrkIsju9-Yo"
REVIEW_URL = f"https://search.google.com/local/reviews?placeid={REVIEW_PLACE_ID}"
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

    def test_short_maps_url_stops_before_adjacent_social_links(self):
        content = (
            f"[Map]({SHORT_URL})"
            "[Facebook](https://www.facebook.com/RotoRooterNewYork/)"
        )

        self.assertEqual(scraper.extract_maps_url_from_content(content), SHORT_URL)

    def test_extracts_place_id_from_google_review_link(self):
        content = f"[Read our Google reviews]({REVIEW_URL})"

        self.assertEqual(scraper.extract_maps_url_from_content(content), REVIEW_URL)
        self.assertEqual(scraper._extract_google_place_id(REVIEW_URL), REVIEW_PLACE_ID)
        self.assertTrue(scraper._has_exact_gbp_identifier(REVIEW_URL))

    def test_extracts_business_identity_from_json_ld(self):
        content = """
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Plumber",
          "name": "Columbia Basin Plumbing",
          "telephone": "+15096195003",
          "address": {"@type": "PostalAddress", "addressLocality": "Kennewick"}
        }
        </script>
        """

        self.assertEqual(
            scraper.extract_business_info(content),
            {
                "name": "Columbia Basin Plumbing",
                "city": "Kennewick",
                "phone": "+15096195003",
            },
        )

    def test_prefers_human_logo_name_over_domain_site_name(self):
        content = """
        <script type="application/ld+json">
        {
          "@type": "Organization",
          "name": "nycityplumbingsolutions.com"
        }
        </script>
        <meta property="og:site_name" content="nycityplumbingsolutions.com" />
        <img class="custom-logo" alt="ny city plumbing solutions logo"
             src="https://nycityplumbingsolutions.com/logo.png" />
        614 49th Street Brooklyn New York
        Call 1800-990-1591
        """

        info = scraper.extract_business_info(content)

        self.assertEqual(info["name"], "ny city plumbing solutions")

    def test_direct_html_conversion_preserves_only_structural_logo_alt(self):
        html_content = """
        <header>
          <a href="/">
            <img class="custom-logo" src="/mentor.jpg" alt="Mentor Mechanical" />
          </a>
          <img class="hero-photo" src="/plumber.jpg" alt="Plumber repairing a boiler" />
        </header>
        <main>Serving New York City.</main>
        """

        readable = scraper._html_to_readable_text(html_content)
        info = scraper.extract_business_info(readable)

        self.assertIn("Mentor Mechanical logo", readable)
        self.assertNotIn("Plumber repairing a boiler", readable)
        self.assertEqual(info["name"], "Mentor Mechanical")

    def test_generic_logo_variant_is_not_used_as_a_business_name(self):
        content = """
        <title>Plumbers NYC – Mentor Mechanical</title>
        <img class="custom-logo logo-white" alt="logo white" src="/white.svg" />
        <meta property="og:site_name" content="Mentor Mechanical" />
        """

        signals = scraper.extract_business_identity_signals(content)
        info = scraper.extract_business_info(content)

        self.assertEqual(info["name"], "Mentor Mechanical")
        self.assertNotIn(
            "logo white",
            [signal.value.lower() for signal in signals if signal.field == "name"],
        )

    def test_markdown_title_and_logo_url_preserve_real_brand_evidence(self):
        content = """
        Title: Mentor Mechanical | Plumbers NYC
        ![Mentor Mechanical](https://example.com/assets/header-logo.svg)
        """

        signals = scraper.extract_business_identity_signals(content)
        info = scraper.extract_business_info(content)

        self.assertEqual(info["name"], "Mentor Mechanical")
        self.assertIn(
            "Mentor Mechanical",
            [signal.value for signal in signals if signal.source == "logo_alt"],
        )

    def test_sentence_fragment_is_not_accepted_as_a_city(self):
        info = scraper.extract_business_info(
            "Our team works in the morning is not always the same. Call today."
        )

        self.assertIsNone(info["city"])

    def test_copyright_owner_is_evidence_but_not_a_standalone_brand(self):
        content = "Copyright © 2026 Site Builder Pro | Powered by Agency Platform"

        signals = scraper.extract_business_identity_signals(content)
        info = scraper.extract_business_info(content)

        self.assertIn(
            "Site Builder Pro",
            [signal.value for signal in signals if signal.source == "copyright_owner"],
        )
        self.assertIsNone(info["name"])

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

    def test_extracts_unique_stable_branch_link_from_location_selector(self):
        page_url = "https://www.rotorooter.com/plumbing/emergency-plumber/"
        branch_url = "https://www.rotorooter.com/manhattan/"
        content = (
            "Enter ZIP, City or Postal Code\n"
            f"[MANHATTAN, NY]({branch_url})\n"
            f"[View Location details]({branch_url})\n"
            "[Locations](https://www.rotorooter.com/locations/)"
        )

        self.assertTrue(scraper._has_dynamic_location_selector(content))
        self.assertEqual(
            scraper._extract_dynamic_location_page_urls(page_url, content),
            [branch_url],
        )

    def test_does_not_guess_between_multiple_location_selector_branches(self):
        page_url = "https://example.com/services/emergency/"
        content = (
            "Select your location\n"
            "[View Location details](/manhattan/)\n"
            "[View Location details](/brooklyn/)"
        )

        self.assertEqual(
            scraper._extract_dynamic_location_page_urls(page_url, content),
            [],
        )

    def test_plain_locations_navigation_is_not_a_dynamic_selector(self):
        content = "[Locations](/locations/) [Contact Us](/contact/)"

        self.assertFalse(scraper._has_dynamic_location_selector(content))
        self.assertEqual(
            scraper._extract_dynamic_location_page_urls(
                "https://example.com/services/emergency/",
                content,
            ),
            [],
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

    async def test_does_not_guess_first_data_id_from_ambiguous_google_body(self):
        ambiguous_body = (
            '<script>nearby="0x1111111111111111:0x2222222222222222";'
            'target="0x3333333333333333:0x4444444444444444"</script>'
        )
        responses = [
            _FakeResponse(url=SHORT_URL, text=ambiguous_body)
            for _ in range(scraper._GBP_LOOKUP_ATTEMPTS)
        ]
        client = _FakeClient(responses)
        diagnostic = {}

        with patch("app.tasks.scraper.httpx.AsyncClient", return_value=client), patch(
            "app.tasks.scraper.asyncio.sleep", new=AsyncMock()
        ):
            resolved = await scraper._resolve_gbp_url(SHORT_URL, diagnostic=diagnostic)

        self.assertEqual(resolved, SHORT_URL)
        self.assertIsNone(scraper._extract_data_id_from_gbp_url(resolved))
        self.assertEqual(diagnostic["code"], "short_url_resolution_failed")

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

    async def test_scrape_uses_stable_dynamic_branch_only_for_gbp_discovery(self):
        page_url = "https://www.rotorooter.com/plumbing/emergency-plumber/"
        branch_url = "https://www.rotorooter.com/manhattan/"
        main_content = (
            "# 24 Hour Emergency Plumbing\n"
            "Enter ZIP, City or Postal Code\n"
            f"[MANHATTAN, NY]({branch_url})\n"
            f"[View Location details]({branch_url})"
        )
        branch_content = """
        # Roto-Rooter Manhattan
        Location: 450 7th Ave Ste B, New York, NY 10123
        Phone: (212) 687-1662
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Plumber",
          "name": "Roto-Rooter Manhattan",
          "telephone": "+12126871662",
          "address": {
            "@type": "PostalAddress",
            "streetAddress": "450 7th Ave Ste B",
            "addressLocality": "New York",
            "addressRegion": "NY",
            "postalCode": "10123"
          }
        }
        </script>
        """
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
        fetch_gbp = AsyncMock(return_value={"name": "RR Plumbing Roto-Rooter"})

        with patch.object(scraper.settings, "FIRECRAWL_API_KEY", ""), patch(
            "app.tasks.scraper.fetch_page_content", fetch_page
        ), patch(
            "app.tasks.scraper.discover_sub_page_urls", return_value=[]
        ), patch(
            "app.tasks.scraper.fetch_gbp_data", fetch_gbp
        ):
            result = await scraper.scrape(page_url)

        lookup = fetch_gbp.await_args.kwargs
        self.assertTrue(lookup["require_location_match"])
        self.assertEqual(lookup["city"], "New York")
        self.assertEqual(lookup["phone"], "+12126871662")
        self.assertIn("450 7th Ave Ste B", lookup["address"])
        self.assertNotIn("450 7th Ave", result["content"])
        self.assertEqual(fetch_page.await_args_list[1].args[0], branch_url)

    async def test_scrape_resolves_user_location_to_verified_branch_page(self):
        page_url = "https://www.rotorooter.com/plumbing/emergency-plumber/"
        branch_url = "https://www.rotorooter.com/manhattan/"
        main_content = """
        # 24 Hour Emergency Plumbing
        Roto-Rooter provides emergency plumbing services nationwide.
        """
        branch_content = """
        # Roto-Rooter Manhattan
        Location: 450 7th Ave Ste B, New York, NY 10123
        Phone: (212) 687-1662
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Plumber",
          "name": "Roto-Rooter Manhattan",
          "telephone": "+12126871662",
          "address": {
            "@type": "PostalAddress",
            "streetAddress": "450 7th Ave Ste B",
            "addressLocality": "New York",
            "addressRegion": "NY",
            "postalCode": "10123"
          }
        }
        </script>
        """
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
        fetch_gbp = AsyncMock(return_value={"name": "RR Plumbing Roto-Rooter"})

        with patch.object(scraper.settings, "FIRECRAWL_API_KEY", ""), patch(
            "app.tasks.scraper.fetch_page_content", fetch_page
        ), patch(
            "app.tasks.scraper.discover_sub_page_urls", return_value=[]
        ), patch(
            "app.tasks.scraper.fetch_gbp_data", fetch_gbp
        ):
            result = await scraper.scrape(
                page_url,
                location_context="  Manhattan,   NY ",
            )

        lookup = fetch_gbp.await_args.kwargs
        self.assertEqual(fetch_page.await_args_list[1].args[0], branch_url)
        self.assertEqual(lookup["business_name"], "Roto-Rooter Manhattan")
        self.assertEqual(lookup["city"], "New York")
        self.assertEqual(lookup["phone"], "+12126871662")
        self.assertIn("450 7th Ave Ste B", lookup["address"])
        self.assertIn("Manhattan, NY", lookup["location_hints"])
        self.assertTrue(lookup["require_location_match"])
        self.assertEqual(result["location_context"], "Manhattan, NY")
        self.assertNotIn("450 7th Ave", result["content"])
        self.assertEqual(result["page_fact_scope"], "verified_location_branch")
        self.assertEqual(result["page_fact_source_url"], branch_url)
        self.assertEqual(result["verified_branch_url"], branch_url)
        self.assertIn("450 7th Ave", result["page_fact_content"])
        self.assertEqual(result["page_fact_business"]["phone"], "+12126871662")
        self.assertTrue(result["page_fact_content_sha256"])

    async def test_unverified_location_branch_cannot_supply_l3_or_gbp_facts(self):
        page_url = "https://example.com/plumbing/emergency-plumber/"
        wrong_branch_url = "https://example.com/brooklyn/"
        main_content = (
            "# Emergency Plumbing\n"
            "Enter ZIP, City or Postal Code\n"
            f"[View Location details]({wrong_branch_url})"
        )
        wrong_branch_content = (
            "# Example Brooklyn\n"
            "102 Atlantic Ave, Brooklyn, NY 11201\n"
            "Phone: (718) 555-0100"
        )
        fetch_page = AsyncMock(side_effect=[
            scraper.ScrapeResult(
                content=main_content,
                source=scraper.ScraperSource.JINA,
                elapsed=0.1,
                content_length=len(main_content),
            ),
            *[
                scraper.ScrapeResult(
                    content=wrong_branch_content,
                    source=scraper.ScraperSource.JINA,
                    elapsed=0.1,
                    content_length=len(wrong_branch_content),
                )
                for _ in range(5)
            ],
        ])
        fetch_gbp = AsyncMock(return_value={"name": "Wrong branch"})

        with patch.object(scraper.settings, "FIRECRAWL_API_KEY", ""), patch(
            "app.tasks.scraper.fetch_page_content", fetch_page
        ), patch(
            "app.tasks.scraper.discover_sub_page_urls", return_value=[]
        ), patch(
            "app.tasks.scraper.fetch_gbp_data", fetch_gbp
        ):
            result = await scraper.scrape(
                page_url,
                location_context="Manhattan, NY",
            )

        fetch_gbp.assert_not_awaited()
        self.assertEqual(result["page_fact_scope"], "submitted_url")
        self.assertEqual(result["page_fact_source_url"], page_url)
        self.assertIsNone(result["verified_branch_url"])
        self.assertNotIn("102 Atlantic Ave", result["page_fact_content"])
        self.assertEqual(
            result["gbp_lookup_diagnostic"]["code"],
            "branch_location_context_missing",
        )

    def test_location_branch_candidates_are_bounded_and_same_domain(self):
        self.assertEqual(
            scraper._location_branch_url_candidates(
                "https://www.rotorooter.com/plumbing/emergency-plumber/",
                "Manhattan, NY",
            ),
            [
                "https://www.rotorooter.com/manhattan/",
                "https://www.rotorooter.com/locations/manhattan/",
                "https://www.rotorooter.com/manhattan-ny/",
                "https://www.rotorooter.com/locations/manhattan-ny/",
            ],
        )

    def test_location_scoped_submitted_url_is_not_replaced_by_branch_root(self):
        self.assertTrue(
            scraper._page_url_contains_location_context(
                "https://www.1tomplumber.com/tri-cities-wa/services/plumbing/",
                "Tri-Cities, WA",
            )
        )
        self.assertFalse(
            scraper._page_url_contains_location_context(
                "https://www.rotorooter.com/plumbing/emergency-plumber/",
                "Manhattan, NY",
            )
        )

    def test_branch_content_requires_location_and_public_identity_anchor(self):
        self.assertFalse(
            scraper._branch_content_matches_location_context(
                "Choose Manhattan from our locations navigation.",
                "Manhattan, NY",
                "https://www.rotorooter.com/manhattan/",
            )
        )
        self.assertFalse(
            scraper._branch_content_matches_location_context(
                "Roto-Rooter Brooklyn | 10233 Topanga Canyon Blvd, Chatsworth, CA 91311 | Phone: (310) 595-1403",
                "Manhattan, NY",
                "https://www.rotorooter.com/manhattan/",
            )
        )
        self.assertTrue(
            scraper._branch_content_matches_location_context(
                "Roto-Rooter Manhattan | 450 7th Ave Ste B, New York, NY 10123 | (212) 687-1662",
                "Manhattan, NY",
                "https://www.rotorooter.com/manhattan/",
            )
        )

    def test_branch_location_can_match_verified_candidate_website_path(self):
        decision = scraper._evaluate_gbp_candidate(
            {
                "title": "RR Plumbing Roto-Rooter",
                "address": "450 7th Ave Ste B, New York, NY 10123",
                "phone": "(212) 687-1215",
                "website": "https://www.rotorooter.com/manhattan/",
            },
            website_url="https://www.rotorooter.com/plumbing/emergency-plumber/",
            business_name="Roto-Rooter",
            phone=None,
            address=None,
            city=None,
            location_hints=["Manhattan, NY"],
            require_location_match=True,
        )

        self.assertTrue(decision["accepted"])
        self.assertIn("location", decision["branch_anchor_matches"])

    async def test_scrape_requires_branch_anchor_when_selector_has_no_stable_link(self):
        page_url = "https://example.com/services/emergency/"
        main_content = (
            "# Emergency Plumbing\n"
            "Enter ZIP, City or Postal Code\n"
            "Choose a location to continue."
        )
        fetch_page = AsyncMock(return_value=scraper.ScrapeResult(
            content=main_content,
            source=scraper.ScraperSource.JINA,
            elapsed=0.1,
            content_length=len(main_content),
        ))
        fetch_gbp = AsyncMock(return_value={})

        with patch.object(scraper.settings, "FIRECRAWL_API_KEY", ""), patch(
            "app.tasks.scraper.fetch_page_content", fetch_page
        ), patch(
            "app.tasks.scraper.discover_sub_page_urls", return_value=[]
        ), patch(
            "app.tasks.scraper.fetch_gbp_data", fetch_gbp
        ):
            result = await scraper.scrape(page_url)

        fetch_gbp.assert_not_awaited()
        self.assertEqual(fetch_page.await_count, 1)
        self.assertEqual(
            result["gbp_lookup_diagnostic"]["code"],
            "branch_location_context_missing",
        )

    async def test_scrape_keeps_supporting_pages_out_of_dify_content(self):
        page_url = "https://example.com/services/emergency/"
        support_url = "https://example.com/about-us/"
        main_content = "# Emergency Plumbing\nTARGET PAGE ONLY\n[About](/about-us/)"
        support_content = """
        # About Our Team
        SUPPORTING PAGE MUST NOT REACH DIFY
        <script type="application/ld+json">
        {
          "@type": "Plumber",
          "name": "Supporting Business Identity",
          "telephone": "+15551234567",
          "address": {"addressLocality": "Tulsa"}
        }
        </script>
        """
        main_result = scraper.ScrapeResult(
            content=main_content,
            source=scraper.ScraperSource.FIRECRAWL,
            elapsed=0.1,
            content_length=len(main_content),
        )
        fetch_gbp = AsyncMock(return_value={})

        with patch.object(scraper.settings, "FIRECRAWL_API_KEY", "test-key"), patch(
            "app.tasks.scraper.fetch_page_content", new=AsyncMock(return_value=main_result)
        ), patch(
            "app.tasks.scraper.firecrawl_map", new=AsyncMock(return_value=[support_url])
        ), patch(
            "app.tasks.scraper.firecrawl_batch_scrape",
            new=AsyncMock(return_value=[{"url": support_url, "markdown": support_content}]),
        ), patch(
            "app.tasks.scraper.fetch_gbp_data", new=fetch_gbp
        ):
            result = await scraper.scrape(page_url)

        self.assertEqual(result["content"], scraper.clean_content(main_content))
        self.assertNotIn("SUPPORTING PAGE MUST NOT REACH DIFY", result["content"])
        self.assertIsNone(result["business"]["phone"])
        self.assertIsNone(fetch_gbp.await_args.kwargs["phone"])
        self.assertIn(
            "+15551234567",
            [
                signal.value
                for signal in fetch_gbp.await_args.kwargs["identity_signals"]
                if signal.field == "phone" and signal.scope == "site_discovery"
            ],
        )
        self.assertEqual(result["raw_content_length"], len(main_content))
        self.assertEqual(result["sub_pages"], [support_url])

    async def test_fallback_scrape_also_keeps_supporting_pages_out_of_content(self):
        page_url = "https://example.com/services/emergency/"
        support_url = "https://example.com/contact/"
        main_content = "# Emergency Plumbing\nTARGET FALLBACK CONTENT"
        support_content = "# Contact\nFALLBACK SUPPORTING CONTENT"
        fetch_page = AsyncMock(side_effect=[
            scraper.ScrapeResult(
                content=main_content,
                source=scraper.ScraperSource.JINA,
                elapsed=0.1,
                content_length=len(main_content),
            ),
            scraper.ScrapeResult(
                content=support_content,
                source=scraper.ScraperSource.JINA,
                elapsed=0.1,
                content_length=len(support_content),
            ),
        ])

        with patch.object(scraper.settings, "FIRECRAWL_API_KEY", ""), patch(
            "app.tasks.scraper.fetch_page_content", new=fetch_page
        ), patch(
            "app.tasks.scraper.discover_sub_page_urls", return_value=[support_url]
        ), patch(
            "app.tasks.scraper.fetch_gbp_data", new=AsyncMock(return_value={})
        ):
            result = await scraper.scrape(page_url)

        self.assertEqual(result["content"], scraper.clean_content(main_content))
        self.assertNotIn("FALLBACK SUPPORTING CONTENT", result["content"])
        self.assertEqual(result["sub_pages"], [support_url])

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
                user_provided_gbp=True,
            )

        self.assertEqual(result["name"], "Spot On Plumbing of Tulsa Plumbers")
        self.assertEqual(result["data_id"], "0x87b68befcc42b925:0x20f8d8fccd659226")
        params = client.requests[0][1]["params"]
        self.assertNotIn("type", params)
        self.assertEqual(params["data_cid"], "2375887383727280678")

    async def test_review_place_id_uses_exact_place_lookup_without_resolution(self):
        place = {
            "title": "Columbia Basin Plumbing",
            "phone": "+1 509-619-5003",
            "address": "7103 W Clearwater Ave B, Kennewick, WA 99336",
            "website": "https://columbiabasinplumbing.com/",
            "data_id": "0x549878f9fd6a3a4d:0x2a39b20ae908ba92",
        }
        client = _FakeClient([
            _FakeResponse(url="https://serpapi.example/search", payload={"place_results": place})
        ])

        with patch.object(scraper.settings, "SERPAPI_KEY", "test-key"), patch(
            "app.tasks.scraper._resolve_gbp_url", new=AsyncMock()
        ) as resolve, patch(
            "app.tasks.scraper.httpx.AsyncClient", return_value=client
        ), patch(
            "app.tasks.scraper._enrich_gbp_info", new=AsyncMock(side_effect=lambda value: value)
        ):
            result = await scraper.fetch_gbp_data(
                business_name="Columbia Basin Plumbing",
                city="Kennewick",
                website_url="https://columbiabasinplumbing.com/repairs-installs/",
                gbp_url=REVIEW_URL,
                user_provided_gbp=True,
            )

        self.assertEqual(result["name"], "Columbia Basin Plumbing")
        resolve.assert_not_awaited()
        params = client.requests[0][1]["params"]
        self.assertEqual(params["place_id"], REVIEW_PLACE_ID)
        self.assertNotIn("data_cid", params)
        self.assertNotIn("q", params)

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
                phone="9188447961",
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
                phone="9188447961",
                diagnostic=diagnostic,
            )

        self.assertEqual(result["name"], "Spot On Plumbing of Tulsa Plumbers")
        self.assertEqual(diagnostic["code"], "strict_domain_fallback_match")
        self.assertTrue(client.requests[0][1]["params"]["q"].endswith("spotonplumbing.com"))

    async def test_branch_strict_domain_fallback_keeps_target_location_in_query(self):
        place = {
            "title": "RR Plumbing Roto-Rooter",
            "phone": "(212) 687-1215",
            "address": "450 7th Ave Ste B, New York, NY 10123",
            "website": "https://www.rotorooter.com/manhattan/",
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
                business_name="Roto-Rooter Manhattan",
                city="New York",
                website_url="https://www.rotorooter.com/plumbing/emergency-plumber/",
                gbp_url=SHORT_URL,
                location_hints=["Manhattan, NY"],
                address="450 7th Ave Ste B, New York, NY 10123",
                require_location_match=True,
                diagnostic=diagnostic,
            )

        self.assertEqual(result["name"], "RR Plumbing Roto-Rooter")
        self.assertEqual(
            client.requests[0][1]["params"]["q"],
            "www.rotorooter.com New York",
        )

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
                phone="9188447961",
                diagnostic=diagnostic,
            )

        self.assertEqual(result["name"], "Spot On Plumbing of Tulsa Plumbers")
        self.assertEqual(diagnostic["code"], "strict_domain_fallback_match")
        self.assertEqual(len(client.requests), 4)

    async def test_malformed_exact_result_uses_verified_domain_fallback(self):
        malformed = {
            "title": "1",
            "type": "Level",
        }
        correct = {
            "title": "A&E NYC Plumbing",
            "phone": "(646) 392-7164",
            "address": "40 Fulton St, New York, NY 10038",
            "website": "https://www.topplumbernyc.com/",
            "data_id": "0x89c25a0000000000:0x1234567890abcdef",
        }
        malformed_response = _FakeResponse(
            url="https://serpapi.example/search",
            payload={"place_results": malformed},
        )
        client = _FakeClient([
            malformed_response,
            malformed_response,
            malformed_response,
            _FakeResponse(
                url="https://serpapi.example/search",
                payload={"place_results": correct},
            ),
        ])
        diagnostic = {}

        with patch.object(scraper.settings, "SERPAPI_KEY", "test-key"), patch(
            "app.tasks.scraper._resolve_gbp_url", new=AsyncMock(return_value=TULSA_URL)
        ), patch("app.tasks.scraper.httpx.AsyncClient", return_value=client), patch(
            "app.tasks.scraper._enrich_gbp_info", new=AsyncMock(side_effect=lambda value: value)
        ), patch("app.tasks.scraper.asyncio.sleep", new=AsyncMock()):
            result = await scraper.fetch_gbp_data(
                business_name="A&E NYC Plumbing",
                city="New York",
                website_url="https://www.topplumbernyc.com/dishwashers/",
                gbp_url=SHORT_URL,
                phone="6463927164",
                diagnostic=diagnostic,
            )

        self.assertEqual(result["name"], "A&E NYC Plumbing")
        self.assertEqual(result["phone"], "(646) 392-7164")
        self.assertEqual(diagnostic["code"], "strict_domain_fallback_match")
        self.assertEqual(len(client.requests), 4)
        self.assertEqual(
            client.requests[-1][1]["params"]["q"],
            "www.topplumbernyc.com",
        )

    async def test_domain_no_result_switches_to_name_query_and_recovers(self):
        place = {
            "title": "Spot On Plumbing of Tulsa Plumbers",
            "phone": "(918) 844-7961",
            "address": "1911 W Reno St, Broken Arrow, OK 74012",
            "website": "https://spotonplumbing.com/",
        }
        client = _FakeClient([
            _FakeResponse(url="https://serpapi.example/search", payload={}),
            _FakeResponse(url="https://serpapi.example/search", payload={"place_results": place}),
            _FakeResponse(url="https://serpapi.example/search", payload={}),
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
                phone="9188447961",
                diagnostic=diagnostic,
            )

        self.assertEqual(result["name"], "Spot On Plumbing of Tulsa Plumbers")
        self.assertEqual(diagnostic["code"], "search_match")
        self.assertEqual(len(client.requests), 3)
        self.assertNotIn("no_cache", client.requests[0][1]["params"])
        self.assertEqual(
            client.requests[0][1]["params"]["q"],
            "spotonplumbing.com Tulsa",
        )
        self.assertEqual(
            client.requests[1][1]["params"]["q"],
            "Spot On Plumbing Tulsa",
        )

    async def test_serpapi_no_results_error_falls_back_to_human_business_name(self):
        correct = {
            "title": "Nyc Plumbing Solutions",
            "phone": "+1 800-990-1591",
            "address": "614 49th St, Brooklyn, NY 11220",
            "website": "https://nycityplumbingsolutions.com/",
        }
        client = _FakeClient([
            _FakeResponse(
                url="https://serpapi.example/search",
                payload={"error": "Google Maps hasn't returned any results for this query."},
            ),
            _FakeResponse(
                url="https://serpapi.example/search",
                payload={"place_results": correct},
            ),
            _FakeResponse(url="https://serpapi.example/search", payload={}),
            _FakeResponse(url="https://serpapi.example/search", payload={}),
        ])
        diagnostic = {}

        with patch.object(scraper.settings, "SERPAPI_KEY", "test-key"), patch(
            "app.tasks.scraper.httpx.AsyncClient", return_value=client
        ), patch(
            "app.tasks.scraper._enrich_gbp_info", new=AsyncMock(side_effect=lambda value: value)
        ):
            result = await scraper.fetch_gbp_data(
                business_name="Nyc Plumbing Solutions",
                city="New York",
                website_url=(
                    "https://nycityplumbingsolutions.com/"
                    "bathroom-plumbing-services/"
                ),
                phone="1800-990-1591",
                address="614 49th Street Brooklyn, NY 11220",
                diagnostic=diagnostic,
            )

        self.assertEqual(result["name"], "Nyc Plumbing Solutions")
        self.assertEqual(diagnostic["status"], "checked")
        self.assertEqual(
            [request[1]["params"]["q"] for request in client.requests],
            [
                "nycityplumbingsolutions.com New York",
                "Nyc Plumbing Solutions New York",
                "8009901591 New York",
                "614 49th Street Brooklyn, NY 11220",
            ],
        )

    async def test_auto_discovery_falls_back_to_phone_after_domain_and_name(self):
        correct = {
            "title": "Nyc Plumbing Solutions",
            "phone": "+1 800-990-1591",
            "address": "614 49th St, Brooklyn, NY 11220",
            "website": "https://nycityplumbingsolutions.com/",
        }
        empty = _FakeResponse(url="https://serpapi.example/search", payload={})
        client = _FakeClient([
            empty,
            empty,
            _FakeResponse(
                url="https://serpapi.example/search",
                payload={"place_results": correct},
            ),
        ])

        with patch.object(scraper.settings, "SERPAPI_KEY", "test-key"), patch(
            "app.tasks.scraper.httpx.AsyncClient", return_value=client
        ), patch(
            "app.tasks.scraper._enrich_gbp_info", new=AsyncMock(side_effect=lambda value: value)
        ):
            result = await scraper.fetch_gbp_data(
                business_name="Nyc Plumbing Solutions",
                city="New York",
                website_url="https://nycityplumbingsolutions.com/services/",
                phone="1800-990-1591",
                address="614 49th Street Brooklyn, NY 11220",
            )

        self.assertEqual(result["name"], "Nyc Plumbing Solutions")
        self.assertEqual(
            client.requests[2][1]["params"]["q"],
            "8009901591 New York",
        )

    async def test_serpapi_no_results_payload_is_not_reported_as_system_error(self):
        no_results = _FakeResponse(
            url="https://serpapi.example/search",
            payload={"error": "Google Maps hasn't returned any results for this query."},
        )
        client = _FakeClient([no_results, no_results, no_results, no_results])
        diagnostic = {}

        with patch.object(scraper.settings, "SERPAPI_KEY", "test-key"), patch(
            "app.tasks.scraper.httpx.AsyncClient", return_value=client
        ):
            result = await scraper.fetch_gbp_data(
                business_name="Nyc Plumbing Solutions",
                city="New York",
                website_url="https://nycityplumbingsolutions.com/services/",
                phone="1800-990-1591",
                address="614 49th Street Brooklyn, NY 11220",
                diagnostic=diagnostic,
            )

        self.assertEqual(result, {})
        self.assertEqual(diagnostic["status"], "not_found")
        self.assertEqual(diagnostic["code"], "search_no_match")

    async def test_transport_failures_remain_a_system_error(self):
        client = _FakeClient([
            TimeoutError("temporary timeout"),
            RuntimeError("SerpAPI unavailable"),
        ])
        diagnostic = {}

        with patch.object(scraper.settings, "SERPAPI_KEY", "test-key"), patch(
            "app.tasks.scraper.httpx.AsyncClient", return_value=client
        ):
            result = await scraper.fetch_gbp_data(
                business_name="Nyc Plumbing Solutions",
                city="New York",
                website_url="https://nycityplumbingsolutions.com/services/",
                diagnostic=diagnostic,
            )

        self.assertEqual(result, {})
        self.assertEqual(diagnostic["status"], "error")
        self.assertEqual(diagnostic["code"], "serpapi_request_failed")

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
                require_location_match=True,
                diagnostic=diagnostic,
            )

        self.assertEqual(result["name"], "1-Tom-Plumber Tri-Cities")
        self.assertEqual(diagnostic["code"], "search_match")
        self.assertIn("Richland", client.requests[0][1]["params"]["q"])

    async def test_domain_search_exhausts_distinct_queries_before_not_found(self):
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
        self.assertEqual(len(client.requests), 2)
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

    def test_single_store_accepts_name_and_city_when_candidate_website_is_missing(self):
        candidate = {
            "title": "Columbia Basin Plumbing",
            "address": "7103 W Clearwater Ave B, Kennewick, WA 99336",
            "phone": "+1 509-619-5003",
            "website": "",
        }

        self.assertTrue(
            scraper._is_confident_gbp_match(
                candidate,
                website_url="https://columbiabasinplumbing.com/repairs-installs/",
                business_name="Columbia Basin Plumbing",
                phone="+1 509-619-5003",
                city="Kennewick",
                location_hints=["Kennewick", "Richland", "Pasco"],
            )
        )

    def test_single_store_rejects_conflicting_candidate_website(self):
        candidate = {
            "title": "Columbia Basin Plumbing",
            "address": "Kennewick, WA",
            "website": "https://different-plumber.example/",
        }

        self.assertFalse(
            scraper._is_confident_gbp_match(
                candidate,
                website_url="https://columbiabasinplumbing.com/repairs-installs/",
                business_name="Columbia Basin Plumbing",
                phone=None,
                city="Kennewick",
                location_hints=["Kennewick"],
            )
        )

    def test_alternate_gbp_website_is_accepted_with_exact_phone_and_name(self):
        candidate = {
            "title": "Doodyman to the Rescue",
            "phone": "+1 516-354-8336",
            "address": "60 Merrick Rd, Rockville Centre, NY 11570",
            "website": "https://thedoodyman.com/",
        }

        self.assertTrue(
            scraper._is_confident_gbp_match(
                candidate,
                website_url="https://doodymantotherescue.com/services/home-plumbing-repair/",
                business_name="Doodyman to the Rescue",
                phone="5163548336",
                city="Rockville Centre",
            )
        )

    def test_name_and_city_without_unique_anchor_are_not_enough(self):
        candidate = {
            "title": "Mentor Mechanical",
            "address": "New York, NY",
            "website": "",
        }

        self.assertFalse(
            scraper._is_confident_gbp_match(
                candidate,
                website_url="https://mentormechanicalcorp.com/plumbers-nyc/",
                business_name="Mentor Mechanical",
                phone=None,
                city="New York",
            )
        )

    def test_single_store_name_and_domain_are_enough_without_using_supporting_phone(self):
        candidate = {
            "title": "Shared Brand Plumbing",
            "phone": "+1 918-555-0100",
            "address": "Tulsa, OK",
            "website": "https://sharedbrand.example/",
        }
        supporting_signals = [
            scraper.IdentitySignal(
                field="phone",
                value="9185550100",
                source="visible_phone",
                quality="strong",
                scope="site_discovery",
            ),
        ]

        self.assertTrue(
            scraper._is_confident_gbp_match(
                candidate,
                website_url="https://sharedbrand.example/services/",
                business_name="Shared Brand Plumbing",
                phone=None,
                city=None,
                identity_signals=supporting_signals,
            )
        )

    def test_equally_strong_candidates_are_left_ambiguous(self):
        candidates = [
            {
                "title": "Example Plumbing",
                "address": "10 Main St, Tulsa, OK",
                "website": "https://exampleplumbing.com/",
                "data_id": "candidate-a",
            },
            {
                "title": "Example Plumbing",
                "address": "20 Main St, Tulsa, OK",
                "website": "https://exampleplumbing.com/",
                "data_id": "candidate-b",
            },
        ]

        selected, decision = scraper._select_verified_gbp_candidate(
            candidates,
            website_url="https://exampleplumbing.com/services/",
            business_name="Example Plumbing",
            phone=None,
            address=None,
            city="Tulsa",
        )

        self.assertIsNone(selected)
        self.assertEqual(decision["status"], "ambiguous")

    def test_user_selected_exact_profile_is_the_comparison_target(self):
        candidate = {
            "title": "Different Public Profile Name",
            "phone": "+1 212-555-0199",
            "address": "New York, NY",
            "website": "https://different.example/",
        }

        self.assertTrue(
            scraper._is_confident_exact_gbp_match(
                candidate,
                website_url="https://page.example/service/",
                business_name="Page Business Name",
                phone="2125550100",
                city="New York",
                user_provided_gbp=True,
            )
        )

    def test_auto_discovered_exact_profile_keeps_phone_difference_for_l3(self):
        candidate = {
            "title": "Example Plumbing",
            "phone": "+1 918-555-0199",
            "address": "Tulsa, OK",
            "website": "https://exampleplumbing.com/",
        }

        self.assertTrue(
            scraper._is_confident_exact_gbp_match(
                candidate,
                website_url="https://exampleplumbing.com/services/",
                business_name="Example Plumbing",
                phone="9185550100",
                city="Tulsa",
                user_provided_gbp=False,
            )
        )

    def test_single_store_accepts_two_identity_matches_and_records_other_differences(self):
        candidate = {
            "title": "Spot On Plumbing of Tulsa Plumbers",
            "phone": "+1 918-844-7961",
            "address": "1911 W Reno St, Broken Arrow, OK 74012",
            "website": "https://spotonplumbing.com/",
        }

        decision = scraper._evaluate_gbp_candidate(
            candidate,
            website_url="https://spotonplumbing.com/emergency-services/",
            business_name="Spot On Plumbing",
            phone="(918) 818-3901",
            address="1911 West Reno Street, Broken Arrow, OK 74012",
            city="Tulsa",
        )

        self.assertTrue(decision["accepted"])
        self.assertGreaterEqual(decision["identity_match_count"], 2)
        self.assertIn("phone_mismatch", decision["differences"])
        self.assertEqual(decision["conflicts"], [])

    def test_candidate_collision_automatically_requires_branch_anchor(self):
        candidates = [
            {
                "title": "1-Tom-Plumber Tulsa",
                "address": "9525 E 51st St Ste G, Tulsa, OK 74145",
                "website": "https://www.1tomplumber.com/",
                "data_id": "tulsa",
            },
            {
                "title": "1-Tom-Plumber Tri-Cities",
                "address": "7103 W Clearwater Ave, Kennewick, WA 99336",
                "website": "https://www.1tomplumber.com/tri-cities-wa/",
                "data_id": "tri-cities",
            },
        ]

        selected, decision = scraper._select_verified_gbp_candidate(
            candidates,
            website_url="https://www.1tomplumber.com/tri-cities-wa/services/plumbing/",
            business_name="1-Tom-Plumber",
            phone=None,
            address=None,
            city="Kennewick",
            location_hints=["Kennewick", "Richland", "Pasco"],
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected["data_id"], "tri-cities")
        self.assertTrue(decision["branch_selection_risk"])

    def test_candidate_collision_without_target_branch_anchor_is_ambiguous(self):
        candidates = [
            {
                "title": "Shared Brand Plumbing Tulsa",
                "address": "10 Main St, Tulsa, OK",
                "website": "https://sharedbrand.example/",
                "data_id": "tulsa",
            },
            {
                "title": "Shared Brand Plumbing Dallas",
                "address": "20 Main St, Dallas, TX",
                "website": "https://sharedbrand.example/",
                "data_id": "dallas",
            },
        ]

        selected, decision = scraper._select_verified_gbp_candidate(
            candidates,
            website_url="https://sharedbrand.example/services/",
            business_name="Shared Brand Plumbing",
            phone=None,
            address=None,
            city=None,
        )

        self.assertIsNone(selected)
        self.assertEqual(decision["status"], "ambiguous")
        self.assertTrue(decision["branch_selection_risk"])

    def test_multi_location_still_rejects_wrong_city_on_shared_domain(self):
        wrong_branch = {
            "title": "1-Tom-Plumber Tulsa",
            "address": "9525 E 51st St Ste G, Tulsa, OK 74145",
            "website": "https://www.1tomplumber.com/",
        }

        self.assertFalse(
            scraper._is_confident_gbp_match(
                wrong_branch,
                website_url="https://www.1tomplumber.com/tri-cities-wa/services/plumbing/",
                business_name="1-Tom-Plumber Tri-Cities",
                phone="509-555-0100",
                city="Richland",
                location_hints=["Kennewick", "Pasco", "West Richland"],
                require_location_match=True,
            )
        )

    def test_multi_location_accepts_unique_phone_when_candidate_address_is_missing(self):
        correct_branch = {
            "title": "1-Tom-Plumber Tri-Cities",
            "address": "",
            "phone": "509-555-0100",
            "website": "https://www.1tomplumber.com/tri-cities-wa/",
        }

        self.assertTrue(
            scraper._is_confident_gbp_match(
                correct_branch,
                website_url="https://www.1tomplumber.com/tri-cities-wa/services/plumbing/",
                business_name="1-Tom-Plumber Tri-Cities",
                phone="(509) 555-0100",
                city="Richland",
                location_hints=["Kennewick", "Pasco", "West Richland"],
                require_location_match=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
