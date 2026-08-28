from app.collectors.site_inventory_html import extract_html_structure
from app.collectors.site_inventory_urls import SiteScope


def test_extracts_bounded_page_structure_and_same_site_links() -> None:
    html = """
    <html>
      <head>
        <title> Acme Plumbing </title>
        <link rel="canonical" href="https://www.example.com/services/plumbing/?utm_source=x">
        <meta name="robots" content="index, follow">
        <script type="application/ld+json">
          {"@context":"https://schema.org","@type":["Plumber","LocalBusiness"]}
        </script>
      </head>
      <body>
        <h1> Emergency Plumbing </h1>
        <a href="/contact/">Contact</a>
        <a href="https://other.example/about">External</a>
        <script>secret script text</script>
        <p>Fast local repairs.</p>
      </body>
    </html>
    """
    scope = SiteScope.from_root("https://example.com/")

    result = extract_html_structure(
        html,
        page_url="https://example.com/services/plumbing",
        scope=scope,
    )

    assert result.title == "Acme Plumbing"
    assert result.h1 == "Emergency Plumbing"
    assert result.canonical_url == "https://example.com/services/plumbing"
    assert result.meta_robots == ("index", "follow")
    assert result.schema_types == ("LocalBusiness", "Plumber")
    assert result.internal_links == ("https://example.com/contact",)
    assert "Fast local repairs." in result.visible_text
    assert "secret script text" not in result.visible_text


def test_ignores_invalid_json_ld_and_deduplicates_links() -> None:
    html = """
    <script type="application/ld+json">not-json</script>
    <a href="/about?utm_campaign=x">About</a>
    <a href="https://www.example.com/about/">About again</a>
    """

    result = extract_html_structure(
        html,
        page_url="https://example.com/",
        scope=SiteScope.from_root("https://example.com/"),
    )

    assert result.schema_types == ()
    assert result.internal_links == ("https://example.com/about",)


def test_extracts_schema_types_from_multiple_json_ld_blocks() -> None:
    html = """
    <script type="application/ld+json">{"@type":"Organization"}</script>
    <script type="application/ld+json">{"@type":"Service"}</script>
    """

    result = extract_html_structure(
        html,
        page_url="https://example.com/",
        scope=SiteScope.from_root("https://example.com/"),
    )

    assert result.schema_types == ("Organization", "Service")


def test_limits_extracted_links_and_visible_text() -> None:
    html = "<p>123456789</p>" + "".join(f'<a href="/{index}">x</a>' for index in range(5))

    result = extract_html_structure(
        html,
        page_url="https://example.com/",
        scope=SiteScope.from_root("https://example.com/"),
        max_links=2,
        max_text_chars=5,
    )

    assert len(result.internal_links) == 2
    assert len(result.visible_text) == 5
