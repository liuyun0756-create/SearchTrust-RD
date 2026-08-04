import unittest
from unittest.mock import AsyncMock, patch

from app.tasks import scraper


class ScraperContentQualityTests(unittest.TestCase):
    def test_accepts_normal_page_with_recaptcha_component(self):
        content = (
            "# Columbia Basin Plumbing\n"
            "Commercial plumbing repairs and installations for the Tri-Cities.\n"
            "Contact our licensed team for water heaters, gas lines, and drains.\n"
            '<script src="gravityformsrecaptcha/frontend.min.js"></script>\n'
            "This form is protected by reCAPTCHA.\n"
        )

        with patch.object(scraper.settings, "SCRAPER_MIN_CONTENT_LENGTH", 100):
            self.assertTrue(scraper._is_valid_content(content))

    def test_accepts_long_real_page_even_when_third_party_assets_add_signals(self):
        content = (
            "# Repairs and installations\n"
            + "Verified service detail for local customers. " * 500
            + "\nCaptcha integration. Enable JavaScript for the contact form."
        )

        self.assertGreater(len(content), scraper._CHALLENGE_PAGE_MAX_LENGTH)
        self.assertTrue(scraper._is_valid_content(content))

    def test_rejects_compact_cloudflare_challenge(self):
        content = (
            "Just a moment. Enable JavaScript and cookies to continue. "
            "Verify you are human. Ray ID: abc123."
        ) * 5

        with patch.object(scraper.settings, "SCRAPER_MIN_CONTENT_LENGTH", 100):
            self.assertFalse(scraper._is_valid_content(content))

    def test_rejects_compact_access_denied_response(self):
        content = "Access denied. Your request cannot be processed. " * 10

        with patch.object(scraper.settings, "SCRAPER_MIN_CONTENT_LENGTH", 100):
            self.assertFalse(scraper._is_valid_content(content))

    def test_rejects_content_below_minimum_length(self):
        with patch.object(scraper.settings, "SCRAPER_MIN_CONTENT_LENGTH", 300):
            self.assertFalse(scraper._is_valid_content("# Short page"))

    def test_direct_html_conversion_removes_recaptcha_scripts_and_keeps_links(self):
        raw_html = """
            <html><head><style>.captcha { display: block; }</style></head>
            <body>
              <main><h1>Commercial Plumbing Repairs</h1>
              <p>Serving customers throughout the Tri-Cities.</p>
              <a href="/contact/">Contact our team</a></main>
              <script src="gravityformsrecaptcha/frontend.min.js"></script>
              <script type="application/ld+json">{"@type":"Plumber"}</script>
            </body></html>
        """

        content = scraper._html_to_readable_text(raw_html)

        self.assertIn("Commercial Plumbing Repairs", content)
        self.assertIn("[Contact our team](/contact/)", content)
        self.assertIn('type="application/ld+json"', content)
        self.assertNotIn("gravityformsrecaptcha", content)
        self.assertNotIn(".captcha", content)

    def test_ssrf_guard_rejects_private_redirect_target(self):
        self.assertFalse(scraper._is_ssrf_safe("http://127.0.0.1/admin"))
        self.assertFalse(scraper._is_ssrf_safe("http://169.254.169.254/latest/meta-data"))


class ScraperWaterfallTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_http_is_used_after_reader_services_fail(self):
        direct_content = "Direct server-rendered page content. " * 20
        with patch.object(
            scraper,
            "_fetch_firecrawl",
            new=AsyncMock(return_value=None),
        ) as firecrawl, patch.object(
            scraper,
            "_fetch_jina",
            new=AsyncMock(return_value=None),
        ) as jina, patch.object(
            scraper,
            "_fetch_direct",
            new=AsyncMock(return_value=direct_content),
        ) as direct:
            result = await scraper.fetch_page_content("https://example.com/service")

        self.assertIsNotNone(result)
        self.assertEqual(result.source, scraper.ScraperSource.DIRECT)
        self.assertEqual(result.content, direct_content)
        firecrawl.assert_awaited_once()
        jina.assert_awaited_once()
        direct.assert_awaited_once()
